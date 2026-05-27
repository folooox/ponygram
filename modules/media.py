"""
Media URL parsing & download module — powered by ParseHub.

Supports 17+ platforms: YouTube, Bilibili, TikTok/Douyin, Instagram,
Twitter/X, 微博, 小红书, 贴吧, Threads, Facebook, 快手, and more.

Trigger: paste any URL → bot sends "⏳ 解析中…" → sends media on success;
silently does nothing for unsupported platforms or failures.

Only active groups (is_active=True) get URL processing.
Private chats always get URL processing.
Per-group dlmode_enabled toggle (default True) controls whether parsing runs.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import tempfile
import traceback
from pathlib import Path
from typing import Optional

from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters

from bot.database import get_group_settings
from bot.logger import get_logger

log = get_logger(__name__)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Quick pre-filter: only attempt parsing for these domains
_KNOWN_DOMAINS = {
    "youtube.com", "youtu.be",
    "bilibili.com", "b23.tv",
    "tiktok.com", "vm.tiktok.com",
    "douyin.com",
    "instagram.com",
    "twitter.com", "x.com", "t.co",
    "xiaohongshu.com", "xhslink.com",
    "weibo.com", "weibo.cn",
    "tieba.baidu.com",
    "facebook.com", "fb.watch",
    "threads.net",
    "kuaishou.com",
    "coolapk.com",
    "pipix.com",
    "zuiyou.com",
    "xiaoheihe.cn",
    "vimeo.com",
    "twitch.tv",
    "nicovideo.jp",
}

_AUTH_REQUIRED_PHRASES = (
    "login required", "unauthorized",
    "http 401", "http 403", "status 401", "status 403", "code 401", "code 403",
    "rate-limit", "rate limit",
    "需要登录", "请先登录",
)

# Maps URL domain substring → BotConfig cookie key
_DOMAIN_COOKIE_KEY: dict[str, str] = {
    "instagram.com":   "cookie_instagram",
    "twitter.com":     "cookie_twitter",
    "x.com":           "cookie_twitter",
    "t.co":            "cookie_twitter",
    "bilibili.com":    "cookie_bilibili",
    "b23.tv":          "cookie_bilibili",
    "douyin.com":      "cookie_douyin",
    "tiktok.com":      "cookie_tiktok",
    "vm.tiktok.com":   "cookie_tiktok",
    "kuaishou.com":    "cookie_kuaishou",
    "xiaohongshu.com": "cookie_xiaohongshu",
    "xhslink.com":     "cookie_xiaohongshu",
    "youtube.com":     "cookie_youtube",
    "youtu.be":        "cookie_youtube",
}

_ph = None


def _is_known_url(url: str) -> bool:
    url_lower = url.lower()
    return any(d in url_lower for d in _KNOWN_DOMAINS)


def _is_auth_error(msg: str) -> bool:
    m = msg.lower()
    return any(p in m for p in _AUTH_REQUIRED_PHRASES)


def _get_ph():
    global _ph
    if _ph is None:
        from parsehub import ParseHub
        _ph = ParseHub()
    return _ph


def _get_proxy() -> Optional[str]:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or None
    )


async def _get_cookie(url: str) -> Optional[str]:
    from bot.database import get_bot_config
    url_lower = url.lower()
    for domain, key in _DOMAIN_COOKIE_KEY.items():
        if domain in url_lower:
            return await get_bot_config(key)
    return None


async def _notify_cookie_expired(context, url: str, err: str = "") -> None:
    try:
        cfg = context.bot_data.get("config")
        owner_id = getattr(cfg, "owner_id", None)
        if not owner_id:
            return
        url_lower = url.lower()
        platform = next(
            (d.split(".")[0].capitalize() for d in _DOMAIN_COOKIE_KEY if d in url_lower),
            "某平台",
        )
        err_block = f"\n原始错误：<code>{html.escape(err[:200])}</code>\n" if err else ""
        await context.bot.send_message(
            owner_id,
            f"⚠️ <b>{platform} Cookie 可能已失效</b>\n\n"
            f"解析 <code>{url[:60]}</code> 时返回需要登录的错误。\n"
            f"{err_block}\n"
            f"建议先诊断：<code>/testcookie {platform.lower()}</code>\n"
            f"确认后更新 Cookie：<code>/setcookie {platform.lower()} &lt;新Cookie&gt;</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Failed to notify owner of cookie expiry", error=str(e))


# ---------------------------------------------------------------------------
# Bilibili direct parser (bypasses parsehub BiliAPI cookie limitation)
# ---------------------------------------------------------------------------

_BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}
_BILI_DOMAINS = ("bilibili.com", "b23.tv", "bili2233.cn")


def _bili_parse_cookie(cookie_str: str) -> dict:
    """Parse 'k=v; k=v' cookie string into dict."""
    out = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def _bili_direct_parse(
    url: str,
    cookie_str: str,
    proxy: Optional[str],
    tmp_dir: str,
) -> tuple[Path, str, str]:
    """
    Direct B站 video download using stored cookie.
    Returns (video_path, title, thumbnail_url). Raises on any failure.
    """
    import httpx

    cookies = _bili_parse_cookie(cookie_str)

    async with httpx.AsyncClient(
        proxy=proxy,
        headers=_BILI_HEADERS,
        cookies=cookies,
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    ) as client:

        # Resolve short links (b23.tv / bili2233.cn)
        if any(x in url.lower() for x in ("b23.tv", "bili2233.cn")):
            resp = await client.get(url)
            url = str(resp.url)
            log.info("Bili short link resolved", final=url)

        # Extract BVID
        m = re.search(r"BV[0-9A-Za-z]{10,}", url)
        if not m:
            raise ValueError(f"Cannot extract BVID from URL: {url}")
        bvid = m.group(0)

        # Step 1: video metadata (cookie 防止 412 风控)
        r = await client.get(
            "https://api.bilibili.com/x/web-interface/view/detail",
            params={"bvid": bvid},
        )
        if r.status_code == 412:
            raise Exception("触发B站风控 (412)，Cookie 可能已失效或 IP 被限")
        info = r.json()
        if not info.get("data"):
            raise Exception(f"获取视频信息失败: code={info.get('code')} msg={info.get('message')}")

        view = info["data"]["View"]
        cid: int = view["cid"]
        title: str = view.get("title", "")
        pic: str = view.get("pic", "")
        duration: int = view.get("duration", 0)
        log.info("Bili video info ok", bvid=bvid, cid=cid, title=title[:40], duration=duration)

        # Step 2: buvid fingerprint
        r2 = await client.get("https://api.bilibili.com/x/frontend/finger/spi")
        spi = r2.json().get("data", {})
        full_cookies = {
            **cookies,
            "buvid3": spi.get("b_3", ""),
            "buvid4": spi.get("b_4", ""),
        }

        # Step 3: playurl (request 1080P, B站 caps to account max)
        r3 = await client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": 80,
                "fnver": 0,
                "fnval": 1,
                "fourk": 1,
                "from_client": "BROWSER",
                "web_location": 1315873,
            },
            cookies=full_cookies,
        )
        pjson = r3.json()
        pdata = pjson.get("data") or {}
        durl = pdata.get("durl", [])
        if not durl:
            raise Exception(
                f"playurl 返回空 durl: code={pjson.get('code')} msg={pjson.get('message')} "
                f"quality={pdata.get('quality')}"
            )

        video_url: str = durl[0].get("url") or ""
        if not video_url:
            backup = durl[0].get("backup_url") or []
            video_url = backup[0] if backup else ""
        if not video_url:
            raise Exception("durl 中无有效 URL")

        quality = pdata.get("quality", 0)
        size_bytes = durl[0].get("size", 0)
        log.info("Bili playurl ok", bvid=bvid, quality=quality, size_mb=round(size_bytes / 1024 / 1024, 1))

        # Step 4: stream download with extended timeout
        output = Path(tmp_dir) / f"{bvid}.mp4"
        dl_client = httpx.AsyncClient(
            proxy=proxy,
            headers=_BILI_HEADERS,
            timeout=httpx.Timeout(10.0, read=300.0),
            follow_redirects=True,
        )
        async with dl_client:
            async with dl_client.stream("GET", video_url) as resp:
                resp.raise_for_status()
                with open(output, "wb") as f:
                    async for chunk in resp.aiter_bytes(131072):
                        f.write(chunk)

        log.info("Bili download ok", path=str(output), size_mb=round(output.stat().st_size / 1024 / 1024, 1))
        return output, title, pic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_thumbnail(result) -> Optional[str]:
    media = getattr(result, "media", None)
    if media is None:
        return None
    items = media if isinstance(media, (list, tuple)) else [media]
    for m in items:
        t = getattr(m, "thumb_url", None)
        if t:
            return t
    return None


def _get_files(dr) -> list[Path]:
    media = getattr(dr, "media", None)
    if media is None:
        return []
    items = media if isinstance(media, (list, tuple)) else [media]
    paths: list[Path] = []
    for m in items:
        p = getattr(m, "path", None)
        if p:
            paths.append(Path(p))
        vp = getattr(m, "video_path", None)
        if vp:
            paths.append(Path(vp))
    return [p for p in paths if p.exists()]


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def _process_url(update: Update, context, url: str) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    status = await msg.reply_text("⏳ 解析中…")
    tmp_dir = tempfile.mkdtemp(prefix="ponygram_")

    try:
        cookie = await _get_cookie(url)
        proxy = _get_proxy()

        # ------------------------------------------------------------------ #
        # Bilibili: direct API parse (parsehub BiliAPI 不传 cookie 导致 412)  #
        # ------------------------------------------------------------------ #
        if any(d in url.lower() for d in _BILI_DOMAINS) and cookie:
            try:
                await status.edit_text("⏳ 解析中… (哔哩哔哩)")
                bili_path, bili_title, bili_pic = await asyncio.wait_for(
                    _bili_direct_parse(url, cookie, proxy, tmp_dir),
                    timeout=180,
                )
                lines: list[str] = []
                if bili_title:
                    lines.append(f"🎬 <b>{html.escape(bili_title[:200])}</b>")
                lines.append(f'🔗 <a href="{url}">哔哩哔哩</a>')
                cap = "\n".join(lines)

                if bili_path.exists():
                    if status:
                        await status.delete()
                        status = None
                    with open(bili_path, "rb") as vf:
                        await context.bot.send_video(
                            chat.id,
                            video=vf,
                            caption=cap[:1024],
                            parse_mode=ParseMode.HTML,
                            supports_streaming=True,
                            reply_to_message_id=msg.message_id,
                        )
                    log.info("Bili direct ok", title=bili_title[:50])
                return
            except Exception as e:
                log.warning("Bili direct parse failed, falling back to parsehub", error=str(e))
                # fall through to parsehub

        # ------------------------------------------------------------------ #
        # Generic parsehub path                                               #
        # ------------------------------------------------------------------ #
        try:
            from parsehub.errors import DownloadError, ParseError, UnknownPlatform
            ph = _get_ph()
        except Exception as e:
            log.warning("ParseHub unavailable", error=str(e))
            await status.delete()
            return

        try:
            result = await asyncio.wait_for(
                ph.parse(url, proxy=proxy, cookie=cookie),
                timeout=30,
            )
        except UnknownPlatform:
            await status.delete()
            return
        except (Exception,) as e:
            err = str(e)
            err_type = type(e).__name__
            log.warning(
                "ParseHub parse failed",
                url=url, error=err, err_type=err_type,
                traceback=traceback.format_exc(),
            )
            if _is_auth_error(err):
                if cookie:
                    await _notify_cookie_expired(context, url, err)
                    await status.edit_text(
                        f"❌ 该平台 Cookie 似乎已失效\n\n"
                        f"原始错误：<code>{html.escape(err[:200])}</code>\n\n"
                        f"<i>使用 /testcookie 诊断或 /setcookie 重新配置</i>",
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await status.edit_text(
                        f"❌ 该平台需要登录才能解析\n\n"
                        f"原始错误：<code>{html.escape(err[:200])}</code>\n\n"
                        f"<i>使用 /setcookie 配置 Cookie</i>",
                        parse_mode=ParseMode.HTML,
                    )
            else:
                await status.edit_text(
                    f"❌ 解析失败（{html.escape(err_type)}）\n\n"
                    f"<code>{html.escape(err[:300])}</code>",
                    parse_mode=ParseMode.HTML,
                )
            return

        platform_name = (
            getattr(getattr(result, "platform", None), "display_name", "")
            or "Media"
        )
        result_type = type(result).__name__
        chat_action = (
            ChatAction.UPLOAD_PHOTO
            if "Image" in result_type or "RichText" in result_type
            else ChatAction.UPLOAD_VIDEO
        )
        await status.edit_text(f"⏳ 解析中… ({platform_name})")
        await context.bot.send_chat_action(chat.id, chat_action)

        files: list[Path] = []
        try:
            dr = await asyncio.wait_for(
                result.download(path=tmp_dir, proxy=proxy),
                timeout=180,
            )
            files = _get_files(dr)
        except (asyncio.TimeoutError, Exception) as e:
            log.warning("ParseHub download failed", url=url, error=str(e))

        title   = (getattr(result, "title",   "") or "").strip()
        content = (getattr(result, "content", "") or "").strip()
        lines2: list[str] = []
        if title:
            lines2.append(f"🎬 <b>{title[:200]}</b>")
        if content:
            lines2.append(f"\n{content[:600]}")
        lines2.append(f'🔗 <a href="{url}">{platform_name}</a>')
        caption = "\n".join(lines2)

        async def _send_info_card(extra: str = "") -> None:
            thumbnail = _get_thumbnail(result)
            if thumbnail:
                try:
                    await context.bot.send_photo(
                        chat.id,
                        photo=thumbnail,
                        caption=(caption + extra)[:1024],
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                    return
                except TelegramError:
                    pass
            await context.bot.send_message(
                chat.id,
                text=(caption + extra)[:4096],
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg.message_id,
                disable_web_page_preview=False,
            )

        if not files:
            if status:
                await status.delete()
                status = None
            await _send_info_card("\n\n⚠️ <i>下载失败，请点击原链接查看</i>")
            return

        sendable = files

        if status:
            await status.delete()
            status = None

        await context.bot.send_chat_action(chat.id, chat_action)

        _img_exts   = {".jpg", ".jpeg", ".png", ".webp"}
        _video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
        _audio_exts = {".mp3", ".m4a", ".ogg", ".opus", ".flac"}

        photos = [f for f in sendable if f.suffix.lower() in _img_exts]
        others = [f for f in sendable if f.suffix.lower() not in _img_exts]

        files_sent = 0

        if len(photos) >= 2:
            fhs = [open(fp, "rb") for fp in photos[:10]]
            try:
                media_group = [
                    InputMediaPhoto(
                        media=fh,
                        caption=caption[:1024] if i == 0 else "",
                        parse_mode=ParseMode.HTML,
                    )
                    for i, fh in enumerate(fhs)
                ]
                await context.bot.send_media_group(
                    chat.id,
                    media=media_group,
                    reply_to_message_id=msg.message_id,
                )
                files_sent += len(fhs)
            finally:
                for fh in fhs:
                    fh.close()
        elif len(photos) == 1:
            with open(photos[0], "rb") as f:
                await context.bot.send_photo(
                    chat.id, photo=f, caption=caption[:1024],
                    parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id,
                )
            files_sent += 1

        first_other = files_sent == 0
        for fp in others[:10 - files_sent]:
            ext = fp.suffix.lower()
            c = caption[:1024] if first_other else ""
            first_other = False
            with open(fp, "rb") as f:
                if ext in _video_exts:
                    await context.bot.send_video(
                        chat.id, video=f, caption=c, parse_mode=ParseMode.HTML,
                        supports_streaming=True, reply_to_message_id=msg.message_id,
                    )
                elif ext in _audio_exts:
                    await context.bot.send_audio(
                        chat.id, audio=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                elif ext == ".gif":
                    await context.bot.send_animation(
                        chat.id, animation=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                else:
                    await context.bot.send_document(
                        chat.id, document=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
            files_sent += 1

        log.info("Media sent", platform=platform_name, chat_id=chat.id, files=files_sent)

    except TelegramError as e:
        log.warning("Media send failed", error=str(e))
        if status:
            try:
                await status.edit_text(f"❌ {e}")
            except TelegramError:
                pass
    except Exception as e:
        log.warning("Media pipeline error", error=str(e))
        if status:
            try:
                await status.delete()
            except TelegramError:
                pass
    finally:
        for f in sorted(Path(tmp_dir).rglob("*"), reverse=True):
            try:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    f.rmdir()
            except OSError:
                pass
        try:
            Path(tmp_dir).rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def on_message_url(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.text:
        return

    if chat.type in ("group", "supergroup"):
        settings = await get_group_settings(chat.id)
        if not settings.is_active:
            return
        if not settings.dlmode_enabled:
            return

    match = _URL_RE.search(msg.text)
    if not match:
        return

    url = match.group(0).rstrip(".,;:!?)'\"")
    if not _is_known_url(url):
        return

    await _process_url(update, context, url)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_message_url,
        ),
        group=20,
    )
    log.info("media module loaded")
