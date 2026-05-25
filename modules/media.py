"""
Media URL parsing & download module — powered by yt-dlp.

Supported platforms (auto-detected from URL):
  YouTube, Bilibili, TikTok / Douyin, Instagram, Twitter/X, and 1000+ other
  sites that yt-dlp supports.

Trigger: paste any URL in a chat → bot immediately sends "⏳ 解析中…" →
  on success: edits to send the media file (or info card if >50 MB);
  on failure: silently deletes the status message.

Only active groups (is_active=True) get URL processing.
Private chats always get URL processing.
Per-group dlmode_enabled toggle (default True) controls whether parsing runs.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid
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

# Match any https?:// URL in a message
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Known platforms matched against URL for display name
_PLATFORM_NAMES = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "bilibili.com": "Bilibili",
    "b23.tv": "Bilibili",
    "tiktok.com": "TikTok",
    "douyin.com": "抖音",
    "instagram.com": "Instagram",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "xiaohongshu.com": "小红书",
    "weibo.com": "微博",
    "nicovideo.jp": "NicoVideo",
    "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch",
    "reddit.com": "Reddit",
    "facebook.com": "Facebook",
    "vm.tiktok.com": "TikTok",
}

# Only auto-process URLs from these known platforms
_SUPPORTED_DOMAINS = set(_PLATFORM_NAMES.keys())


def _detect_platform(url: str) -> Optional[str]:
    """Return display name for a URL's platform, or None if not supported."""
    url_lower = url.lower()
    for domain, name in _PLATFORM_NAMES.items():
        if domain in url_lower:
            return name
    return None


# ---------------------------------------------------------------------------
# yt-dlp helpers (blocking — run in thread pool)
# ---------------------------------------------------------------------------

def _ydl_opts(out_path: str) -> list[dict]:
    base = {
        "outtmpl": out_path,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
    }
    return [
        {**base, "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]", "merge_output_format": "mp4"},
        {**base, "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best", "merge_output_format": "mp4"},
        {**base, "format": "worstvideo+bestaudio/worst"},
    ]


_AUTH_REQUIRED_PHRASES = ("login required", "requires authentication", "age-restricted",
                          "private video", "login to confirm", "not available", "rate-limit")


def _is_auth_error(msg: str) -> bool:
    m = msg.lower()
    return any(p in m for p in _AUTH_REQUIRED_PHRASES)


def _run_ydl(url: str, out_path: str) -> Optional[dict]:
    import yt_dlp
    for opts in _ydl_opts(out_path):
        for f in Path(out_path).parent.glob(Path(out_path).stem + "*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            err_str = str(e)
            log.warning("yt-dlp attempt failed", error=err_str)
            if _is_auth_error(err_str):
                return None  # no point retrying other formats
            continue
        except BaseException as e:
            log.warning("yt-dlp attempt failed (base)", error=str(e))
            return None
        candidates = sorted(
            Path(out_path).parent.glob(Path(out_path).stem + "*"),
            key=lambda p: p.stat().st_size if p.exists() else 0,
            reverse=True,
        )
        if not candidates:
            continue
        file_path = candidates[0]
        size = file_path.stat().st_size
        if size <= _TG_MAX_BYTES:
            return {"info": info, "file_path": str(file_path), "size": size}
    return None


def _run_ydl_info(url: str) -> Optional[dict]:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 15}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("yt-dlp info failed", error=str(e))
        return None
    except BaseException as e:
        log.warning("yt-dlp info failed (base)", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _build_caption(info: dict, platform: str) -> str:
    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel", "")
    duration = _fmt_duration(info.get("duration"))
    view_count = info.get("view_count")
    webpage = info.get("webpage_url", "")

    lines = [f"🎬 <b>{title}</b>"]
    if uploader:
        lines.append(f"👤 {uploader}")
    meta = []
    if duration:
        meta.append(f"⏱ {duration}")
    if view_count:
        meta.append(f"👁 {view_count:,}")
    if meta:
        lines.append("  ".join(meta))
    if webpage:
        lines.append(f'\n🔗 <a href="{webpage}">{platform}</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def _process_url(update: Update, context, url: str) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    platform = _detect_platform(url) or "Video"
    status = await msg.reply_text(f"⏳ 解析中… ({platform})")

    loop = asyncio.get_event_loop()
    tmp_dir = tempfile.mkdtemp(prefix="ponygram_")
    out_stem = os.path.join(tmp_dir, uuid.uuid4().hex)

    try:
        await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_ydl, url, out_stem),
                timeout=180,
            )
        except asyncio.TimeoutError:
            log.warning("yt-dlp download timed out", url=url)
            result = None

        if result is None:
            # Try to at least get info for a fallback card
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_ydl_info, url),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                info = None
            if info:
                caption = _build_caption(info, platform)
                caption += "\n\n⚠️ <i>File exceeds 50 MB — link above.</i>"
                thumbnail = info.get("thumbnail")
                if thumbnail:
                    try:
                        await status.delete()
                        await context.bot.send_photo(
                            chat.id, photo=thumbnail, caption=caption[:1024],
                            parse_mode=ParseMode.HTML,
                            reply_to_message_id=msg.message_id,
                        )
                        return
                    except TelegramError:
                        pass
                await status.edit_text(caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
            else:
                # Silently delete the status message on complete failure
                try:
                    await status.delete()
                except TelegramError:
                    pass
            return

        file_path = result["file_path"]
        info = result["info"] or {}
        caption = _build_caption(info, platform) if info else f"🎬 {platform}"
        if len(caption) > 1024:
            caption = caption[:1021] + "…"

        ext = Path(file_path).suffix.lower()
        await status.delete()
        await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)

        with open(file_path, "rb") as f:
            if ext in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
                await context.bot.send_video(
                    chat.id, video=f, caption=caption,
                    parse_mode=ParseMode.HTML, supports_streaming=True,
                    reply_to_message_id=msg.message_id,
                )
            elif ext in (".mp3", ".m4a", ".ogg", ".opus", ".flac"):
                await context.bot.send_audio(
                    chat.id, audio=f, caption=caption,
                    parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id,
                )
            else:
                await context.bot.send_document(
                    chat.id, document=f, caption=caption,
                    parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id,
                )
        log.info("Media sent", platform=platform, chat_id=chat.id)

    except TelegramError as e:
        log.warning("Media send failed", error=str(e))
        try:
            await status.edit_text(f"❌ {e}")
        except TelegramError:
            pass
    except Exception as e:
        log.warning("Media pipeline error", error=str(e))
        try:
            await status.delete()
        except TelegramError:
            pass
    finally:
        for f in Path(tmp_dir).glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def on_message_url(update: Update, context) -> None:
    """Auto-detect media URLs and trigger the download pipeline."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.text:
        return

    # In groups: require activation + dlmode enabled
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
    if not _detect_platform(url):
        return  # skip unknown/unsupported domains

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
