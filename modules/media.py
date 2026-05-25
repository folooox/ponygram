"""
Media parsing & download module — powered by yt-dlp.

Supported platforms (auto-detected from URL):
  YouTube, Bilibili, TikTok / Douyin (watermark-free), Instagram,
  Twitter / X, and 1000+ other sites yt-dlp supports.

Commands:
  /dl <url>    — download and send a video/audio
  /dlinfo <url> — show media info without downloading

Auto-detection:
  Any group/private message containing a supported URL is intercepted
  and the bot offers a "Download" inline button. Toggle per-chat with
  /dlmode on|off (admin only).

Telegram limits:
  50 MB max upload via bot API. If the file is larger the bot tries
  progressively lower quality until it fits. If nothing fits it sends
  the direct stream URL + thumbnail instead (graceful fallback).
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.database import get_group_settings, set_group_field
from bot.logger import get_logger
from bot.permissions import admin_only
from bot.router import registry

log = get_logger(__name__)

_TG_MAX_BYTES = 49 * 1024 * 1024   # 49 MB safety margin below Telegram's 50 MB

# Regex that matches URLs we want to intercept for auto-detect
_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:www\.)?youtube\.com/|youtu\.be/|"
    r"(?:www\.)?bilibili\.com/|b23\.tv/|"
    r"(?:www\.)?tiktok\.com/|(?:www\.)?douyin\.com/|vm\.tiktok\.com/|"
    r"(?:www\.)?instagram\.com/(?:p|reel|tv)/|"
    r"(?:www\.)?twitter\.com/|(?:www\.)?x\.com/"
    r")[^\s]*",
    re.IGNORECASE,
)

# Platform display names
_PLATFORM_NAMES = {
    "youtube": "YouTube",
    "youtu.be": "YouTube",
    "bilibili": "Bilibili",
    "b23.tv": "Bilibili",
    "tiktok": "TikTok",
    "douyin": "抖音",
    "instagram": "Instagram",
    "twitter": "Twitter/X",
    "x.com": "Twitter/X",
}


def _detect_platform(url: str) -> str:
    url_lower = url.lower()
    for key, name in _PLATFORM_NAMES.items():
        if key in url_lower:
            return name
    return "Video"


# ---------------------------------------------------------------------------
# yt-dlp helpers (run in thread pool to stay non-blocking)
# ---------------------------------------------------------------------------

def _ydl_opts_for_size(out_path: str, max_bytes: int) -> list[dict]:
    """
    Return a list of yt-dlp option dicts to try in order, from best to worst quality.
    Each attempt targets a smaller format to stay under max_bytes.
    """
    base = {
        "outtmpl": out_path,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
    }
    attempts = [
        # 1. Best video ≤ 720p + best audio, merged to mp4
        {**base, "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]", "merge_output_format": "mp4"},
        # 2. 480p
        {**base, "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best", "merge_output_format": "mp4"},
        # 3. Worst available (audio-only fallback)
        {**base, "format": "worstvideo+bestaudio/worst"},
    ]
    return attempts


def _run_ydl(url: str, out_path: str) -> Optional[dict]:
    """
    Blocking: try each quality tier until a file fits within _TG_MAX_BYTES.
    Returns yt-dlp info dict on success, None on failure.
    """
    import yt_dlp  # local import — not installed in all envs

    for opts in _ydl_opts_for_size(out_path, _TG_MAX_BYTES):
        # Remove previous attempt file if any
        for f in Path(out_path).parent.glob(Path(out_path).stem + "*"):
            try:
                f.unlink()
            except OSError:
                pass

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            log.warning("yt-dlp download attempt failed", error=str(e), url=url)
            continue

        # Find the actual output file (yt-dlp may add extension)
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
        # Too large — try next tier
        log.debug("File too large, retrying with lower quality", size_mb=size // 1024 // 1024)

    return None


def _run_ydl_info(url: str) -> Optional[dict]:
    """Blocking: extract info without downloading."""
    import yt_dlp
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("yt-dlp info extraction failed", error=str(e))
        return None


async def _extract_info(url: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_ydl_info, url)


async def _download(url: str, out_path: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_ydl, url, out_path)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_size(bytes_: int) -> str:
    if bytes_ >= 1024 ** 3:
        return f"{bytes_ / 1024 ** 3:.1f} GB"
    if bytes_ >= 1024 ** 2:
        return f"{bytes_ / 1024 ** 2:.1f} MB"
    return f"{bytes_ / 1024:.1f} KB"


def _build_info_text(info: dict, platform: str) -> str:
    title = info.get("title", "Unknown")
    uploader = info.get("uploader") or info.get("channel", "")
    duration = _fmt_duration(info.get("duration"))
    view_count = info.get("view_count")
    like_count = info.get("like_count")
    description = info.get("description", "")
    if description and len(description) > 200:
        description = description[:200].rstrip() + "…"
    webpage = info.get("webpage_url", "")

    lines = [f"🎬 <b>{title}</b>"]
    if uploader:
        lines.append(f"👤 {uploader}")

    meta = []
    if duration:
        meta.append(f"⏱ {duration}")
    if view_count:
        meta.append(f"👁 {view_count:,}")
    if like_count:
        meta.append(f"👍 {like_count:,}")
    if meta:
        lines.append("  ".join(meta))

    if description:
        lines.append(f"\n{description}")
    if webpage:
        lines.append(f'\n🔗 <a href="{webpage}">{platform}</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core send logic
# ---------------------------------------------------------------------------

async def _send_media(update: Update, context, url: str, reply_to_msg=None) -> None:
    """Download url and send to chat. reply_to_msg is the message to reply to."""
    msg = reply_to_msg or update.effective_message
    chat = update.effective_chat
    assert msg and chat

    platform = _detect_platform(url)
    status_msg = await msg.reply_text(f"⏳ Downloading from {platform}…")

    await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)

    tmp_dir = tempfile.mkdtemp(prefix="ponygram_")
    out_stem = os.path.join(tmp_dir, uuid.uuid4().hex)

    try:
        result = await _download(url, out_stem)

        if result is None:
            # Fallback: extract info and send link
            info = await _extract_info(url)
            if info:
                text = _build_info_text(info, platform)
                text += "\n\n⚠️ <i>File exceeds 50 MB limit — direct download link above.</i>"
                thumbnail = info.get("thumbnail")
                if thumbnail:
                    try:
                        await status_msg.delete()
                        await context.bot.send_photo(
                            chat.id, photo=thumbnail, caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_to_message_id=msg.message_id,
                        )
                        return
                    except TelegramError:
                        pass
                await status_msg.edit_text(text, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=False)
            else:
                await status_msg.edit_text(
                    f"❌ Could not download from {platform}. The URL may be private, "
                    f"geo-restricted, or unsupported."
                )
            return

        file_path = result["file_path"]
        info = result["info"] or {}
        size = result["size"]

        caption = _build_info_text(info, platform) if info else f"🎬 {platform}"
        # Telegram caption limit is 1024 chars
        if len(caption) > 1024:
            caption = caption[:1021] + "…"

        ext = Path(file_path).suffix.lower()
        await status_msg.delete()
        await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)

        with open(file_path, "rb") as f:
            if ext in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
                await context.bot.send_video(
                    chat.id,
                    video=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    reply_to_message_id=msg.message_id,
                )
            elif ext in (".mp3", ".m4a", ".ogg", ".opus", ".flac"):
                await context.bot.send_audio(
                    chat.id,
                    audio=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=msg.message_id,
                )
            else:
                await context.bot.send_document(
                    chat.id,
                    document=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=msg.message_id,
                )

        log.info("Media sent", platform=platform, size=_fmt_size(size), chat_id=chat.id)

    except TelegramError as e:
        log.warning("Failed to send media", error=str(e))
        await status_msg.edit_text(f"❌ Failed to send file: {e}")
    finally:
        # Clean up temp files
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
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_dl(update: Update, context) -> None:
    """/dl <url> — download and send media."""
    msg = update.effective_message
    assert msg

    if not context.args:
        await msg.reply_text(
            "Usage: /dl <url>\n\n"
            "Supported: YouTube, Bilibili, TikTok, Douyin, Instagram, Twitter/X, and more."
        )
        return

    url = context.args[0]
    if not url.startswith("http"):
        await msg.reply_text("Please provide a valid URL starting with http:// or https://")
        return

    await _send_media(update, context, url)


async def cmd_dlinfo(update: Update, context) -> None:
    """/dlinfo <url> — show media info without downloading."""
    msg = update.effective_message
    assert msg

    if not context.args:
        await msg.reply_text("Usage: /dlinfo <url>")
        return

    url = context.args[0]
    platform = _detect_platform(url)
    status = await msg.reply_text(f"🔍 Fetching info from {platform}…")

    info = await _extract_info(url)
    if not info:
        await status.edit_text("❌ Could not fetch media info. The URL may be private or unsupported.")
        return

    text = _build_info_text(info, platform)
    thumbnail = info.get("thumbnail")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Download", callback_data=f"dl:{url[:200]}")
    ]])

    if thumbnail:
        try:
            await status.delete()
            await context.bot.send_photo(
                update.effective_chat.id,
                photo=thumbnail,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                reply_to_message_id=msg.message_id,
            )
            return
        except TelegramError:
            pass

    await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                            disable_web_page_preview=True)


@admin_only
async def cmd_dlmode(update: Update, context) -> None:
    """/dlmode on|off — toggle auto-detection of media URLs in this chat."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    if not context.args or context.args[0].lower() not in ("on", "off"):
        settings = await get_group_settings(chat.id)
        state = "on" if getattr(settings, "dlmode_enabled", False) else "off"
        await msg.reply_text(
            f"Auto media detection is currently <b>{state}</b>.\n"
            f"Use /dlmode on or /dlmode off to change.",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = context.args[0].lower() == "on"
    await set_group_field(chat.id, dlmode_enabled=enabled)
    await msg.reply_text(
        f"✅ Auto media detection {'enabled' if enabled else 'disabled'}."
    )


# ---------------------------------------------------------------------------
# Auto-detect URL in messages
# ---------------------------------------------------------------------------

async def on_message_url(update: Update, context) -> None:
    """Intercept messages with supported URLs and offer a download button."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.text:
        return

    settings = await get_group_settings(chat.id)
    if not getattr(settings, "dlmode_enabled", False):
        return

    match = _URL_RE.search(msg.text)
    if not match:
        return

    url = match.group(0)
    platform = _detect_platform(url)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⬇️ Download from {platform}", callback_data=f"dl:{url[:200]}")
    ]])
    await msg.reply_text(
        f"🔗 {platform} link detected.",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Inline button: Download
# ---------------------------------------------------------------------------

async def on_dl_button(update: Update, context) -> None:
    query = update.callback_query
    assert query
    await query.answer("Starting download…")

    url = query.data[len("dl:"):]
    await _send_media(update, context, url, reply_to_msg=query.message)


# ---------------------------------------------------------------------------
# Module setup — also add dlmode_enabled column if missing
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    registry.register_command("dl", cmd_dl, "Download video/audio from URL")
    registry.register_command("dlinfo", cmd_dlinfo, "Show media info from URL")
    registry.register_command("dlmode", cmd_dlmode, "Toggle auto URL detection (admin)", admin_only=True)

    application.add_handler(CommandHandler("dl", cmd_dl))
    application.add_handler(CommandHandler("dlinfo", cmd_dlinfo))
    application.add_handler(CommandHandler("dlmode", cmd_dlmode))
    application.add_handler(CallbackQueryHandler(on_dl_button, pattern=r"^dl:"))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_message_url,
        ),
        group=20,
    )

    log.info("media module loaded")
