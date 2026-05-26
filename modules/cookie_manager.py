"""
Cookie health-check scheduler + /testcookie diagnostic command.

Scheduled job (every COOKIE_CHECK_INTERVAL hours, default 6):
  - Lightweight HTTP test per platform.
  - If expired: notify owner with update instructions.
  - No Playwright auto-refresh (unreliable on server IPs due to platform
    cookie-binding to login IP; selector breakage on page redesigns).

Commands (owner only):
  /testcookie <platform>   — parse a known test URL with the stored cookie,
                             show raw result or error so you can diagnose
  /checkcookies            — run health-check immediately for all platforms
"""

from __future__ import annotations

import asyncio
import html
import os
import traceback
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.database import get_all_bot_configs, get_bot_config, set_bot_config
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

# A known public URL per platform for /testcookie
_TEST_URLS: dict[str, str] = {
    "instagram":   "https://www.instagram.com/instagram/",
    "twitter":     "https://x.com/x",
    "bilibili":    "https://www.bilibili.com/video/BV1GJ411x7h7",
    "youtube":     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "tiktok":      "https://www.tiktok.com/@tiktok/video/7106594312292453675",
    "douyin":      "https://www.douyin.com/video/7376812765601591595",
    "kuaishou":    "https://www.kuaishou.com/short-video/3xkmwfe4kqhmmym",
    "xiaohongshu": "https://www.xiaohongshu.com/explore",
}

_SLUG_TO_COOKIE_KEY: dict[str, str] = {
    "instagram":   "cookie_instagram",
    "twitter":     "cookie_twitter",
    "bilibili":    "cookie_bilibili",
    "youtube":     "cookie_youtube",
    "tiktok":      "cookie_tiktok",
    "douyin":      "cookie_douyin",
    "kuaishou":    "cookie_kuaishou",
    "xiaohongshu": "cookie_xiaohongshu",
}

_PLATFORM_ALIAS = {
    "x":       "twitter",
    "ins":     "instagram",
    "xhs":     "xiaohongshu",
    "小红书":   "xiaohongshu",
    "抖音":     "douyin",
    "快手":     "kuaishou",
    "b站":      "bilibili",
}


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
        "User-Agent": "Mozilla/5.0",
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
    headers = {"Cookie": cookie, "User-Agent": "Mozilla/5.0"}
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


async def _check_one(cookie_key: str, cookie_value: str) -> Optional[bool]:
    checker = _CHECKERS.get(cookie_key)
    if not checker:
        return None
    return await checker(cookie_value)


# ---------------------------------------------------------------------------
# Background job
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
    expired = []

    for cookie_key in cookie_keys:
        cookie_value = configs.get(cookie_key, "")
        if not cookie_value:
            continue

        platform_display = _PLATFORM_DISPLAY[cookie_key]
        result = await _check_one(cookie_key, cookie_value)

        if result is True:
            await set_bot_config(f"status_{cookie_key}", "valid")
            log.info("Cookie healthy", platform=platform_display)
        elif result is False:
            await set_bot_config(f"status_{cookie_key}", "expired")
            expired.append(platform_display)
            log.warning("Cookie expired", platform=platform_display)
        # None = no reliable test for this platform, leave status unchanged

    if expired and owner_id:
        platforms_str = "、".join(expired)
        note = (
            "⚠️ 注意：Instagram/Twitter 的 Cookie 通常与登录时的 IP 绑定，"
            "在不同 IP 的服务器上使用会被平台拒绝，这是平台的安全机制。\n"
            "建议通过浏览器代理或直接在服务器 IP 登录后重新导出 Cookie。"
        )
        try:
            await bot.send_message(
                owner_id,
                f"⚠️ <b>Cookie 失效提醒</b>\n\n"
                f"以下平台 Cookie 健康检测失败：<b>{platforms_str}</b>\n\n"
                f"请重新获取 Cookie 并更新：\n"
                f"<code>/setcookie &lt;平台&gt; &lt;新Cookie&gt;</code>\n\n"
                f"{note}\n\n"
                f"用 <code>/testcookie &lt;平台&gt;</code> 可诊断具体错误。",
                parse_mode="HTML",
            )
        except Exception as e:
            log.warning("Failed to notify owner of expired cookies", error=str(e))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@owner_only
async def cmd_testcookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /testcookie <platform> [url] — test stored cookie with ParseHub."""
    msg = update.effective_message
    args = context.args or []

    if not args:
        platforms = " ".join(_SLUG_TO_COOKIE_KEY.keys())
        await msg.reply_text(
            "用法：<code>/testcookie &lt;平台&gt; [自定义URL]</code>\n\n"
            f"支持的平台：<code>{platforms}</code>\n\n"
            "会用存储的 Cookie 尝试解析该平台的一个测试链接，并显示原始结果或错误信息。",
            parse_mode="HTML",
        )
        return

    raw_platform = args[0].lower()
    platform = _PLATFORM_ALIAS.get(raw_platform, raw_platform)
    cookie_key = _SLUG_TO_COOKIE_KEY.get(platform)

    if not cookie_key:
        await msg.reply_text(f"❌ 未知平台：<code>{raw_platform}</code>", parse_mode="HTML")
        return

    cookie = await get_bot_config(cookie_key)
    if not cookie:
        await msg.reply_text(
            f"❌ 未配置 {_PLATFORM_DISPLAY.get(cookie_key, platform)} 的 Cookie\n\n"
            f"请先：<code>/setcookie {platform} &lt;Cookie字符串&gt;</code>",
            parse_mode="HTML",
        )
        return

    test_url = args[1] if len(args) > 1 else _TEST_URLS.get(platform, "")
    if not test_url:
        await msg.reply_text(f"❌ 请提供测试 URL：<code>/testcookie {platform} &lt;URL&gt;</code>", parse_mode="HTML")
        return

    display = _PLATFORM_DISPLAY.get(cookie_key, platform)
    status_msg = await msg.reply_text(
        f"⏳ 正在用 {display} Cookie 解析测试链接…\n<code>{test_url[:80]}</code>",
        parse_mode="HTML",
    )

    try:
        from parsehub import ParseHub
        from parsehub.errors import UnknownPlatform, ParseError
        ph = ParseHub()

        result = await asyncio.wait_for(
            ph.parse(test_url, cookie=cookie),
            timeout=30,
        )

        title = (getattr(result, "title", "") or "").strip()
        platform_name = getattr(getattr(result, "platform", None), "display_name", display)
        result_type = type(result).__name__

        await status_msg.edit_text(
            f"✅ <b>Cookie 有效！ParseHub 解析成功</b>\n\n"
            f"平台：{platform_name}\n"
            f"结果类型：{result_type}\n"
            f"标题：{title[:100] or '（无）'}\n\n"
            f"Cookie 可正常工作 🎉",
            parse_mode="HTML",
        )

    except asyncio.TimeoutError:
        await status_msg.edit_text(
            f"⏱ <b>解析超时（30s）</b>\n\n"
            f"可能原因：网络慢、平台需要更多时间、或 Cookie 无效导致卡在认证。",
            parse_mode="HTML",
        )
    except Exception as e:
        err = str(e)
        err_type = type(e).__name__
        tb = traceback.format_exc()
        log.warning(
            "testcookie ParseHub failure",
            url=test_url, err=err, err_type=err_type, traceback=tb,
        )

        cause = e.__cause__ or e.__context__
        cause_str = ""
        while cause:
            cause_str += f"\n↳ {type(cause).__name__}: {str(cause)[:200]}"
            nxt = cause.__cause__ or cause.__context__
            if nxt is cause:
                break
            cause = nxt

        cause_html = (
            f"\n\n<b>底层原因</b>：<code>{html.escape(cause_str[:400])}</code>"
            if cause_str else ""
        )
        await status_msg.edit_text(
            f"❌ <b>解析失败（{html.escape(err_type)}）</b>\n\n"
            f"错误：<code>{html.escape(err[:300])}</code>"
            f"{cause_html}\n\n"
            f"💡 用 <code>/debugparse {html.escape(test_url)}</code> 绕过 ParseHub 直接调用 yt-dlp 拿真错误",
            parse_mode="HTML",
        )


@owner_only
async def cmd_checkcookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger cookie health check for all platforms."""
    msg = update.effective_message
    configs = await get_all_bot_configs()
    cookie_keys = [k for k in configs if k.startswith("cookie_") and k in _PLATFORM_DISPLAY]

    if not cookie_keys:
        await msg.reply_text("没有配置任何平台 Cookie。")
        return

    status_msg = await msg.reply_text(f"⏳ 正在检测 {len(cookie_keys)} 个平台的 Cookie…")

    results = []
    for cookie_key in cookie_keys:
        cookie_value = configs.get(cookie_key, "")
        if not cookie_value:
            continue
        display = _PLATFORM_DISPLAY[cookie_key]
        result = await _check_one(cookie_key, cookie_value)

        if result is True:
            await set_bot_config(f"status_{cookie_key}", "valid")
            results.append(f"🟢 <b>{display}</b>  有效")
        elif result is False:
            await set_bot_config(f"status_{cookie_key}", "expired")
            results.append(f"🔴 <b>{display}</b>  已失效")
        else:
            results.append(f"⬜ <b>{display}</b>  无法检测（该平台无可靠测试端点）")

    await status_msg.edit_text(
        "<b>Cookie 健康检测结果</b>\n\n" + "\n".join(results) + "\n\n"
        "用 <code>/testcookie &lt;平台&gt;</code> 可做 ParseHub 实际解析测试。",
        parse_mode="HTML",
    )


def setup(application: Application) -> None:
    application.add_handler(CommandHandler("testcookie", cmd_testcookie))
    application.add_handler(CommandHandler("checkcookies", cmd_checkcookies))

    interval_secs = _CHECK_INTERVAL_HOURS * 3600
    application.job_queue.run_repeating(
        check_all_cookies,
        interval=interval_secs,
        first=300,  # first check 5 minutes after startup
        name="cookie_health_check",
    )
    log.info("cookie_manager module loaded", check_interval_hours=_CHECK_INTERVAL_HOURS)
