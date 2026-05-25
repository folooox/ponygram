"""
Cookie health-check scheduler + Playwright credential-based auto-refresh.

Scheduled job (every COOKIE_CHECK_INTERVAL hours, default 6):
  1. For each stored cookie with a defined test, run a lightweight HTTP check.
  2. If expired and credentials exist → auto-refresh via Playwright.
  3. If expired and no credentials → notify owner.

Commands (owner only):
  /setcredentials <platform> <username> <password>
  /delcredentials <platform>
  /listcredentials
  /refreshcookie <platform>   — manual Playwright refresh
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.database import (
    delete_bot_config,
    get_all_bot_configs,
    get_all_credentials,
    get_credential,
    set_bot_config,
    set_credential,
    delete_credential,
    update_credential_refresh,
)
from bot.logger import get_logger
from bot.permissions import owner_only

log = get_logger(__name__)

_CHECK_INTERVAL_HOURS = int(os.environ.get("COOKIE_CHECK_INTERVAL", "6"))

_PLATFORM_DISPLAY: dict[str, str] = {
    "cookie_instagram":   "Instagram",
    "cookie_twitter":     "Twitter/X",
    "cookie_bilibili":    "Bilibili",
    "cookie_youtube":     "YouTube",
    "cookie_tiktok":      "TikTok",
    "cookie_douyin":      "抖音",
    "cookie_kuaishou":    "快手",
    "cookie_xiaohongshu": "小红书",
}

_COOKIE_KEY_TO_SLUG: dict[str, str] = {
    "cookie_instagram":   "instagram",
    "cookie_twitter":     "twitter",
    "cookie_bilibili":    "bilibili",
    "cookie_youtube":     "youtube",
    "cookie_tiktok":      "tiktok",
    "cookie_douyin":      "douyin",
    "cookie_kuaishou":    "kuaishou",
    "cookie_xiaohongshu": "xiaohongshu",
}

# Platforms where Playwright auto-login is implemented
_PW_SUPPORTED = {"instagram", "twitter", "bilibili", "tiktok"}

# Platforms supporting auto-refresh (shown in /listcredentials and web UI)
_CREDENTIAL_PLATFORMS: dict[str, str] = {
    "instagram": "Instagram",
    "twitter":   "Twitter/X",
    "bilibili":  "Bilibili",
    "tiktok":    "TikTok",
}

_PLATFORM_ALIAS = {"x": "twitter", "ins": "instagram", "b站": "bilibili"}


# ---------------------------------------------------------------------------
# Encryption helpers (Fernet, key derived from bot token)
# ---------------------------------------------------------------------------

def _derive_key(bot_token: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(bot_token.encode()).digest())


def encrypt_password(bot_token: str, password: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_derive_key(bot_token)).encrypt(password.encode()).decode()


def decrypt_password(bot_token: str, encrypted: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_derive_key(bot_token)).decrypt(encrypted.encode()).decode()


# ---------------------------------------------------------------------------
# Health checks — lightweight HTTP test per platform
# ---------------------------------------------------------------------------

async def _check_instagram(cookie: str) -> Optional[bool]:
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "X-IG-App-ID": "936619743392459",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://i.instagram.com/api/v1/accounts/current_user/?edit=true",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status == 401:
                    return False
                if r.status == 200:
                    return True
    except Exception as e:
        log.warning("Instagram health check error", error=str(e))
    return None


async def _check_twitter(cookie: str) -> Optional[bool]:
    # Extract ct0 value needed for X-CSRF-Token
    ct0 = None
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == "ct0":
            ct0 = v.strip()
            break
    if not ct0:
        return None

    headers = {
        "Cookie": cookie,
        "X-Csrf-Token": ct0,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        # Public bearer token used by the Twitter web app
        "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.twitter.com/1.1/account/settings.json",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status in (401, 403):
                    return False
                if r.status == 200:
                    return True
    except Exception as e:
        log.warning("Twitter health check error", error=str(e))
    return None


async def _check_bilibili(cookie: str) -> Optional[bool]:
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status != 200:
                    return None
                text = await r.text()
                if '"isLogin":true' in text:
                    return True
                if '"isLogin":false' in text:
                    return False
    except Exception as e:
        log.warning("Bilibili health check error", error=str(e))
    return None


async def _check_youtube(cookie: str) -> Optional[bool]:
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.youtube.com/",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15), ssl=False,
                allow_redirects=True,
            ) as r:
                if r.status != 200:
                    return None
                text = await r.text()
                if '"LOGGED_IN"' in text or '"isSignedIn":true' in text:
                    return True
    except Exception as e:
        log.warning("YouTube health check error", error=str(e))
    return None


async def _check_xiaohongshu(cookie: str) -> Optional[bool]:
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.xiaohongshu.com/",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status in (401, 461):
                    return False
                if r.status == 200:
                    text = await r.text()
                    return "success" in text.lower()
    except Exception as e:
        log.warning("Xiaohongshu health check error", error=str(e))
    return None


_CHECKERS = {
    "cookie_instagram":   _check_instagram,
    "cookie_twitter":     _check_twitter,
    "cookie_bilibili":    _check_bilibili,
    "cookie_youtube":     _check_youtube,
    "cookie_xiaohongshu": _check_xiaohongshu,
}


async def _check_cookie(cookie_key: str, cookie_value: str) -> Optional[bool]:
    """Return True=valid, False=expired, None=no reliable test for this platform."""
    checker = _CHECKERS.get(cookie_key)
    if not checker:
        return None
    return await checker(cookie_value)


# ---------------------------------------------------------------------------
# Playwright auto-refresh
# ---------------------------------------------------------------------------

async def _pw_launch():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    return pw, browser


async def _pw_instagram(username: str, password: str) -> Optional[str]:
    pw, browser = await _pw_launch()
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
    )
    page = await ctx.new_page()
    try:
        await page.goto("https://www.instagram.com/accounts/login/", timeout=30000)
        try:
            await page.click("text=Allow all cookies", timeout=4000)
        except Exception:
            pass
        await page.wait_for_selector('input[name="username"]', timeout=15000)
        await page.fill('input[name="username"]', username)
        await page.fill('input[name="password"]', password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(
            lambda u: "login" not in u and "challenge" not in u, timeout=25000
        )
        cookies = await ctx.cookies()
        result = "; ".join(
            f"{c['name']}={c['value']}"
            for c in cookies if "instagram.com" in c.get("domain", "")
        )
        return result or None
    finally:
        await browser.close()
        await pw.stop()


async def _pw_twitter(username: str, password: str) -> Optional[str]:
    pw, browser = await _pw_launch()
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    page = await ctx.new_page()
    try:
        await page.goto("https://x.com/i/flow/login", timeout=30000)
        await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
        await page.fill('input[autocomplete="username"]', username)
        await page.click('[data-testid="LoginForm_Next_Button"]')
        await page.wait_for_selector('input[name="password"]', timeout=10000)
        await page.fill('input[name="password"]', password)
        await page.click('[data-testid="LoginForm_Login_Button"]')
        await page.wait_for_url(lambda u: "flow/login" not in u, timeout=20000)
        cookies = await ctx.cookies()
        result = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return result or None
    finally:
        await browser.close()
        await pw.stop()


async def _pw_bilibili(username: str, password: str) -> Optional[str]:
    pw, browser = await _pw_launch()
    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        await page.goto("https://passport.bilibili.com/login", timeout=30000)
        await page.wait_for_selector("#login-username", timeout=15000)
        await page.fill("#login-username", username)
        await page.fill("#login-password", password)
        await page.click(".btn_primary")
        await page.wait_for_url(
            lambda u: "passport.bilibili.com" not in u, timeout=20000
        )
        cookies = await ctx.cookies()
        result = "; ".join(
            f"{c['name']}={c['value']}"
            for c in cookies if "bilibili.com" in c.get("domain", "")
        )
        return result or None
    finally:
        await browser.close()
        await pw.stop()


async def _pw_tiktok(username: str, password: str) -> Optional[str]:
    pw, browser = await _pw_launch()
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    page = await ctx.new_page()
    try:
        await page.goto(
            "https://www.tiktok.com/login/phone-or-email/email", timeout=30000
        )
        await page.wait_for_selector('input[name="username"]', timeout=15000)
        await page.fill('input[name="username"]', username)
        await page.fill('input[type="password"]', password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(lambda u: "login" not in u, timeout=20000)
        cookies = await ctx.cookies()
        result = "; ".join(
            f"{c['name']}={c['value']}"
            for c in cookies if "tiktok.com" in c.get("domain", "")
        )
        return result or None
    finally:
        await browser.close()
        await pw.stop()


_PW_LOGINS = {
    "instagram": _pw_instagram,
    "twitter":   _pw_twitter,
    "bilibili":  _pw_bilibili,
    "tiktok":    _pw_tiktok,
}


async def playwright_refresh(platform: str, username: str, password: str) -> Optional[str]:
    """Run Playwright login and return new cookie string, or None on failure."""
    try:
        from playwright.async_api import async_playwright  # noqa — verify installed
    except ImportError:
        log.warning("Playwright not installed, skipping auto-refresh")
        return None

    login_fn = _PW_LOGINS.get(platform)
    if not login_fn:
        return None

    try:
        return await asyncio.wait_for(login_fn(username, password), timeout=120)
    except asyncio.TimeoutError:
        log.warning("Playwright login timed out", platform=platform)
        return None
    except Exception as e:
        log.warning("Playwright login failed", platform=platform, error=str(e))
        return None


# ---------------------------------------------------------------------------
# Background health-check job
# ---------------------------------------------------------------------------

async def check_all_cookies(context) -> None:
    bot = context.bot
    cfg = context.bot_data.get("config")
    owner_id = getattr(cfg, "owner_id", None)

    configs = await get_all_bot_configs()
    cookie_keys = [k for k in configs if k.startswith("cookie_") and k in _PLATFORM_DISPLAY]
    if not cookie_keys:
        return

    log.info("Cookie health check starting", count=len(cookie_keys))

    for cookie_key in cookie_keys:
        cookie_value = configs.get(cookie_key, "")
        if not cookie_value:
            continue

        platform_display = _PLATFORM_DISPLAY[cookie_key]
        platform_slug = _COOKIE_KEY_TO_SLUG[cookie_key]

        result = await _check_cookie(cookie_key, cookie_value)

        if result is True:
            await set_bot_config(f"status_{cookie_key}", "valid")
            log.info("Cookie healthy", platform=platform_display)
            continue

        if result is None:
            # No reliable test for this platform; rely on failure detection in media.py
            continue

        # Cookie is expired
        log.warning("Cookie expired detected by health check", platform=platform_display)
        await set_bot_config(f"status_{cookie_key}", "expired")

        # Try Playwright auto-refresh first
        refreshed = False
        if platform_slug in _PW_SUPPORTED:
            cred = await get_credential(platform_slug)
            if cred and bot.token:
                try:
                    pwd = decrypt_password(bot.token, cred.password_enc)
                except Exception:
                    pwd = None
                if pwd:
                    log.info("Attempting Playwright auto-refresh", platform=platform_display)
                    new_cookie = await playwright_refresh(platform_slug, cred.username, pwd)
                    if new_cookie:
                        await set_bot_config(cookie_key, new_cookie)
                        await set_bot_config(f"status_{cookie_key}", "valid")
                        await update_credential_refresh(platform_slug, ok=True)
                        refreshed = True
                        log.info("Cookie auto-refreshed successfully", platform=platform_display)
                        if owner_id:
                            try:
                                await bot.send_message(
                                    owner_id,
                                    f"✅ <b>{platform_display} Cookie 已自动刷新</b>\n\n"
                                    f"Playwright 自动登录成功，新 Cookie 已保存。",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                    else:
                        await update_credential_refresh(platform_slug, ok=False)

        if not refreshed and owner_id:
            try:
                msg = (
                    f"⚠️ <b>{platform_display} Cookie 已失效</b>\n\n"
                    f"定时健康检查（每 {_CHECK_INTERVAL_HOURS}h）发现 Cookie 无法通过验证。\n\n"
                    f"请手动更新：\n"
                    f"<code>/setcookie {platform_slug} &lt;新Cookie&gt;</code>"
                )
                if platform_slug in _PW_SUPPORTED and not await get_credential(platform_slug):
                    msg += (
                        f"\n\n💡 配置账号密码可实现自动刷新：\n"
                        f"<code>/setcredentials {platform_slug} &lt;用户名&gt; &lt;密码&gt;</code>"
                    )
                await bot.send_message(owner_id, msg, parse_mode="HTML")
            except Exception as e:
                log.warning("Failed to notify owner of expired cookie", error=str(e))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@owner_only
async def cmd_setcredentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /setcredentials <platform> <username> <password>"""
    msg = update.effective_message
    args = context.args or []
    if len(args) < 3:
        platforms = " ".join(_CREDENTIAL_PLATFORMS.keys())
        await msg.reply_text(
            "用法：<code>/setcredentials &lt;平台&gt; &lt;用户名&gt; &lt;密码&gt;</code>\n\n"
            f"支持自动刷新的平台：<code>{platforms}</code>\n\n"
            "密码会用 AES 加密存储在数据库中。",
            parse_mode="HTML",
        )
        return

    platform = _PLATFORM_ALIAS.get(args[0].lower(), args[0].lower())
    username = args[1]
    password = " ".join(args[2:])

    if platform not in _CREDENTIAL_PLATFORMS:
        await msg.reply_text(
            f"❌ 不支持自动刷新的平台：<code>{platform}</code>\n\n"
            f"支持：{' '.join(_CREDENTIAL_PLATFORMS.keys())}",
            parse_mode="HTML",
        )
        return

    try:
        encrypted = encrypt_password(context.bot.token, password)
    except ImportError:
        await msg.reply_text("❌ cryptography 未安装，无法加密凭据。请检查依赖。")
        return

    await set_credential(platform, username, encrypted)
    display = _CREDENTIAL_PLATFORMS[platform]
    log.info("Credentials saved", platform=display)
    await msg.reply_text(
        f"✅ <b>{display}</b> 账号凭据已保存\n\n"
        f"用户名：<code>{username}</code>\n"
        f"密码已加密存储。\n\n"
        f"Cookie 失效时将自动用 Playwright 刷新，也可手动执行：\n"
        f"<code>/refreshcookie {platform}</code>",
        parse_mode="HTML",
    )


@owner_only
async def cmd_delcredentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text("用法：<code>/delcredentials &lt;平台&gt;</code>", parse_mode="HTML")
        return

    platform = _PLATFORM_ALIAS.get(args[0].lower(), args[0].lower())
    await delete_credential(platform)
    display = _CREDENTIAL_PLATFORMS.get(platform, platform)
    await msg.reply_text(f"🗑 {display} 账号凭据已删除")


@owner_only
async def cmd_listcredentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    creds = await get_all_credentials()
    cred_map = {c.platform: c for c in creds}

    lines = ["<b>账号凭据状态（自动刷新）</b>\n"]
    for slug, display in _CREDENTIAL_PLATFORMS.items():
        cred = cred_map.get(slug)
        if cred:
            if cred.last_refresh_ok is True:
                status = "🟢 上次刷新成功"
            elif cred.last_refresh_ok is False:
                status = "🔴 上次刷新失败"
            else:
                status = "⬜ 从未触发"
            lines.append(
                f"🔑 <b>{display}</b>  {status}\n"
                f"   用户名：<code>{cred.username}</code>"
            )
        else:
            lines.append(f"⬜ <b>{display}</b>  未配置")

    await msg.reply_text("\n".join(lines), parse_mode="HTML")


@owner_only
async def cmd_refreshcookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /refreshcookie <platform> — manually trigger Playwright refresh."""
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text(
            "用法：<code>/refreshcookie &lt;平台&gt;</code>\n\n"
            f"支持：{' '.join(_CREDENTIAL_PLATFORMS.keys())}",
            parse_mode="HTML",
        )
        return

    platform = _PLATFORM_ALIAS.get(args[0].lower(), args[0].lower())
    display = _CREDENTIAL_PLATFORMS.get(platform, platform)

    if platform not in _PW_SUPPORTED:
        await msg.reply_text(f"❌ {display} 不支持 Playwright 自动刷新")
        return

    cred = await get_credential(platform)
    if not cred:
        await msg.reply_text(
            f"❌ 未配置 {display} 的账号凭据\n\n"
            f"请先：<code>/setcredentials {platform} &lt;用户名&gt; &lt;密码&gt;</code>",
            parse_mode="HTML",
        )
        return

    status_msg = await msg.reply_text(f"⏳ 正在用 Playwright 刷新 {display} Cookie…")

    try:
        pwd = decrypt_password(context.bot.token, cred.password_enc)
    except Exception as e:
        await status_msg.edit_text(f"❌ 解密凭据失败：{e}")
        return

    new_cookie = await playwright_refresh(platform, cred.username, pwd)
    cookie_key = f"cookie_{platform}"

    if new_cookie:
        await set_bot_config(cookie_key, new_cookie)
        await set_bot_config(f"status_{cookie_key}", "valid")
        await update_credential_refresh(platform, ok=True)
        await status_msg.edit_text(
            f"✅ <b>{display} Cookie 刷新成功</b>\n新 Cookie 已保存（{len(new_cookie)} 字符）",
            parse_mode="HTML",
        )
    else:
        await update_credential_refresh(platform, ok=False)
        await status_msg.edit_text(
            f"❌ <b>{display} Cookie 刷新失败</b>\n\n"
            f"请检查账号密码是否正确，或手动更新 Cookie：\n"
            f"<code>/setcookie {platform} &lt;新Cookie&gt;</code>",
            parse_mode="HTML",
        )


def setup(application: Application) -> None:
    application.add_handler(CommandHandler("setcredentials", cmd_setcredentials))
    application.add_handler(CommandHandler("delcredentials", cmd_delcredentials))
    application.add_handler(CommandHandler("listcredentials", cmd_listcredentials))
    application.add_handler(CommandHandler("refreshcookie", cmd_refreshcookie))

    interval_secs = _CHECK_INTERVAL_HOURS * 3600
    application.job_queue.run_repeating(
        check_all_cookies,
        interval=interval_secs,
        first=120,  # first check 2 minutes after startup
        name="cookie_health_check",
    )
    log.info("cookie_manager module loaded", check_interval_hours=_CHECK_INTERVAL_HOURS)
