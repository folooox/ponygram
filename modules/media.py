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
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters

from bot.database import get_group_settings
from bot.logger import get_logger

log = get_logger(__name__)

_TG_MAX_BYTES = 49 * 1024 * 1024

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

_AUTH_REQUIRED_PHRASES = ("login required", "401", "403", "unauthorized",
                          "rate-limit", "rate limit", "please wait",
                          "需要登录", "解析错误", "无法获取")

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


async def _get_cookie(url: str) -> Optional[str]:
    """Return the stored cookie for the URL's platform, or None."""
    from bot.database import get_bot_config
    url_lower = url.lower()
    for domain, key in _DOMAIN_COOKIE_KEY.items():
        if domain in url_lower:
            return await get_bot_config(key)
    return None


async def _notify_cookie_expired(context, url: str) -> None:
    """Send the owner a DM when a configured cookie stops working."""
    try:
        cfg = context.bot_data.get("config")
        owner_id = getattr(cfg, "owner_id", None)
        if not owner_id:
            return
        # Guess platform name from URL
        url_lower = url.lower()
        platform = next(
            (d.split(".")[0].capitalize() for d in _DOMAIN_COOKIE_KEY if d in url_lower),
            "某平台",
        )
        await context.bot.send_message(
            owner_id,
            f"⚠️ <b>{platform} Cookie 已失效</b>\n\n"
            f"解析 <code>{url[:60]}</code> 时收到 401/403。\n\n"
            f"请重新登录后更新 Cookie：\n"
            f"<code>/setcookie {platform.lower()} &lt;新Cookie&gt;</code>",
            parse_mode="HTML",
        )
        log.warning("Cookie expired, owner notified", platform=platform)
    except Exception as e:
        log.warning("Failed to notify owner of cookie expiry", error=str(e))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_thumbnail(result) -> Optional[str]:
    """Return the first thumbnail URL found in a parse result."""
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
    """Extract all downloaded file paths from a DownloadResult."""
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
        try:
            from parsehub.errors import DownloadError, ParseError, UnknownPlatform
            ph = _get_ph()
        except Exception as e:
            log.warning("ParseHub unavailable", error=str(e))
            await status.delete()
            return

        # Fetch stored cookie for this platform (may be None)
        cookie = await _get_cookie(url)

        # Parse metadata
        try:
            result = await asyncio.wait_for(
                ph.parse(url, cookie=cookie) if cookie else ph.parse(url),
                timeout=30,
            )
        except UnknownPlatform:
            await status.delete()
            return
        except (Exception,) as e:
            err = str(e)
            log.warning("ParseHub parse failed", url=url, error=err)
            if _is_auth_error(err):
                if cookie:
                    # Cookie was configured but is now invalid — notify owner
                    await _notify_cookie_expired(context, url)
                    await status.edit_text(
                        "❌ Cookie 已失效，已通知管理员更新\n"
                        "<i>使用 /setcookie 重新配置</i>",
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await status.edit_text(
                        "❌ 该平台需要登录才能解析\n"
                        "<i>使用 /setcookie 配置 Cookie，或在后台 Settings 页面填写</i>",
                        parse_mode=ParseMode.HTML,
                    )
            else:
                await status.delete()
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

        # Download files to temp dir
        files: list[Path] = []
        try:
            dr = await asyncio.wait_for(
                result.download(path=tmp_dir),
                timeout=180,
            )
            files = _get_files(dr)
        except (asyncio.TimeoutError, DownloadError, Exception) as e:
            log.warning("ParseHub download failed", url=url, error=str(e))

        # Build caption
        title = (getattr(result, "title", "") or "").strip()
        lines: list[str] = []
        if title:
            lines.append(f"🎬 <b>{title[:200]}</b>")
        lines.append(f'\n🔗 <a href="{url}">{platform_name}</a>')
        caption = "\n".join(lines)

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

        sendable = [f for f in files if f.stat().st_size <= _TG_MAX_BYTES]
        if not sendable:
            if status:
                await status.delete()
                status = None
            await _send_info_card("\n\n⚠️ <i>文件超过 50 MB，请点击原链接查看</i>")
            return

        if status:
            await status.delete()
            status = None

        await context.bot.send_chat_action(chat.id, chat_action)

        for i, fp in enumerate(sendable[:10]):
            ext = fp.suffix.lower()
            c = caption[:1024] if i == 0 else ""
            with open(fp, "rb") as f:
                if ext in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
                    await context.bot.send_video(
                        chat.id, video=f, caption=c, parse_mode=ParseMode.HTML,
                        supports_streaming=True, reply_to_message_id=msg.message_id,
                    )
                elif ext in (".mp3", ".m4a", ".ogg", ".opus", ".flac"):
                    await context.bot.send_audio(
                        chat.id, audio=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                elif ext == ".gif":
                    await context.bot.send_animation(
                        chat.id, animation=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                    await context.bot.send_photo(
                        chat.id, photo=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                else:
                    await context.bot.send_document(
                        chat.id, document=f, caption=c, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )

        log.info("Media sent", platform=platform_name, chat_id=chat.id, files=len(sendable))

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
    """Auto-detect media URLs and trigger the ParseHub pipeline."""
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
        return  # skip random links silently

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
