"""
Media URL parsing & download module — powered by ParseHub (2.0.x).

Supports 17+ platforms: YouTube, Bilibili, TikTok/Douyin, Instagram,
Twitter/X, 微博, 小红书, 贴吧, Threads, Facebook, 快手, and more.

Trigger: paste any URL → bot sends "⏳ 解析中…" → sends media on success;
silently does nothing for unsupported platforms or failures.

Only active groups (is_active=True) get URL processing.
Private chats always get URL processing.
Per-group dlmode_enabled toggle (default True) controls whether parsing runs.
Per-group media_del_original toggle (default True) controls whether the user's
original link message is deleted after the parsed media is sent.

Design — "align with what ParseHub can do":
    Everything goes through ParseHub's own parse + download pipeline
    (``ph.parse(url) → result.download()``). ParseHub already injects the
    correct per-platform download headers (e.g. Bilibili referer) and does
    Range-resume downloads, so we no longer hand-roll platform downloaders.
    Two small, well-contained shims remain because ParseHub can't do them
    itself:
      * Bilibili — inject the configured cookie into the ``view/detail`` call
        so it survives HTTP 412 risk-control (ParseHub omits the cookie there).
      * 小红书 (XHS) — rewrite expiring ``sns-webpic`` image URLs to the stable
        ``ci.xiaohongshu.com/{traceId}`` form, which serves the full image.
    yt-dlp's format is patched to stay within Telegram's upload limit.
"""

from __future__ import annotations

import asyncio
import contextvars
import html
import os
import re
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, MessageHandler, filters

from bot.components import Component, ConfigField, is_module_enabled, register_component
from bot.database import get_group_settings, log_media_error
from bot.logger import get_logger
from bot.telegraph import (
    _ensure_telegraph_token,
    _first_paragraph,
    _md_to_telegraph_nodes,
    _post_to_telegraph,
)

log = get_logger(__name__)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Telegram cloud Bot API upload ceiling is ~50 MB; keep a small margin.
try:
    _DL_MAX_MB = int(os.environ.get("DL_MAX_MB", "49"))
except ValueError:
    _DL_MAX_MB = 49

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
    "mp.weixin.qq.com",
}

_AUTH_REQUIRED_PHRASES = (
    "login required", "unauthorized", "not configured",
    "http 401", "http 403", "status 401", "status 403", "code 401", "code 403",
    "rate-limit", "rate limit",
    "需要登录", "请先登录", "登录后查看", "未配置", "cookie 未配置",
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

_LINK_EMOJI_ID: str | None = None

# XHS CDN serves images only with a mobile UA + referer.
_XHS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    # ci.xiaohongshu.com gzip-compresses JPEGs and reports the *compressed*
    # Content-Length, which trips ParseHub's strict length check ("下载不完整").
    # Asking for identity makes it omit Content-Length, so the check is skipped.
    "Accept-Encoding": "identity",
}


def _is_known_url(url: str) -> bool:
    url_lower = url.lower()
    return any(d in url_lower for d in _KNOWN_DOMAINS)


def _is_auth_error(msg: str) -> bool:
    m = msg.lower()
    return any(p in m for p in _AUTH_REQUIRED_PHRASES)


def _get_proxy() -> Optional[str]:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or None
    )


def _url_host(url: str) -> str:
    """Best-effort host label for the error log (e.g. 'www.youtube.com')."""
    m = re.match(r"https?://([^/]+)", url, re.IGNORECASE)
    return (m.group(1) if m else url)[:64]


def _parse_cookie_str(cookie_str: str) -> dict:
    out: dict[str, str] = {}
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def _fail_silently(
    status,
    url: str,
    stage: str,
    err: Exception | str,
    chat_id: Optional[int] = None,
    context=None,
) -> None:
    """Record a media failure to the web error log and quietly remove the status
    message — failures are never surfaced in chat (per product decision).

    When ``context`` is provided, also push a throttled alert to the owner for
    failures on cookie-bearing platforms so cookie problems are noticed without
    digging through the web error log."""
    err_type = type(err).__name__ if isinstance(err, Exception) else ""
    err_msg = str(err)
    log.info("Media failed silently", url=url, stage=stage, error=err_msg[:200])
    try:
        await log_media_error(
            url=url, platform=_url_host(url), stage=stage,
            error_type=err_type, error_msg=err_msg, chat_id=chat_id,
        )
    except Exception as e:
        log.warning("Failed to record media error", error=str(e))
    if context is not None and stage in ("parse", "download", "send"):
        await _alert_owner_media_failure(context, url, stage, err_msg)
    if status is not None:
        try:
            await status.delete()
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# ParseHub monkeypatches (applied once, idempotent)
# ---------------------------------------------------------------------------

# yt-dlp policy: stay inside Telegram's upload ceiling. ParseHub hardcodes
# "mp4+bestvideo[height<=1080]+bestaudio" (no "/" fallback → "Requested format
# is not available"); the bare "bv*+ba/b" we used before grabbed uncapped 4K
# that blew past 50 MB and failed on send. This ladder prefers ≤1080p that fits
# the size budget, then any ≤720p, then ≤1080p of any size; "/b" can never come
# up empty. A post-download size guard still skips anything that slips over.
_YTDLP_FORMAT = (
    f"bv*[height<=1080][filesize<{_DL_MAX_MB}M]+ba/"
    f"b[height<=1080][filesize<{_DL_MAX_MB}M]/"
    f"bv*[height<=1080][filesize_approx<{_DL_MAX_MB}M]+ba/"
    f"bv*[height<=720]+ba/b[height<=720]/"
    f"bv*[height<=1080]+ba/b"
)
_PATCHED = False

# Per-request Bilibili cookie for the get_video_info shim. A ContextVar keeps
# concurrent parses isolated (each PTB handler runs in its own task).
_bili_cookie_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "bili_cookie", default=None
)


def _patch_parsehub() -> None:
    """Apply yt-dlp format + Bilibili cookie shims. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return

    # --- yt-dlp format ---
    try:
        from parsehub.parsers.base.ytdlp import YtParser

        _orig_params = YtParser.params.fget

        def _params_with_fallback(self):
            params = _orig_params(self)
            params["format"] = _YTDLP_FORMAT
            # The android/web innertube clients are far more tolerant of
            # datacenter/WARP exit IPs than the default web client, which on a
            # flagged IP returns no formats ("Requested format is not
            # available"). The "youtube" key is ignored by other extractors.
            params["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
            return params

        YtParser.params = property(_params_with_fallback)
        log.info("Patched parsehub yt-dlp format", format=_YTDLP_FORMAT)
    except Exception as e:
        log.warning("Could not patch parsehub yt-dlp format", error=str(e))

    # --- Bilibili cookie injection (bypass HTTP 412 risk-control) ---
    try:
        from parsehub.provider_api.bilibili import BiliAPI

        _orig_view = BiliAPI.get_video_info

        async def _view_with_cookie(self, url):
            cookies = _bili_cookie_ctx.get()
            if not cookies:
                return await _orig_view(self, url)
            bvid = self.get_bvid(url)
            if not bvid:
                raise ValueError(f"Invalid url: {url}")
            r = await self._get_client().get(
                "https://api.bilibili.com/x/web-interface/view/detail",
                params={"bvid": bvid},
                cookies=cookies,
            )
            if r.status_code == 412:
                raise Exception("由于触发哔哩哔哩安全风控策略，该次访问请求被拒绝。")
            r.raise_for_status()
            return r.json()

        BiliAPI.get_video_info = _view_with_cookie
        log.info("Patched parsehub Bilibili cookie injection")
    except Exception as e:
        log.warning("Could not patch parsehub Bilibili cookie", error=str(e))

    _PATCHED = True


async def _get_cookie(url: str) -> Optional[str]:
    from bot.database import get_bot_config
    url_lower = url.lower()
    for domain, key in _DOMAIN_COOKIE_KEY.items():
        if domain in url_lower:
            return await get_bot_config(key)
    return None


async def _init_link_emoji(context) -> None:
    global _LINK_EMOJI_ID
    try:
        ss = await context.bot.get_sticker_set("Milo2_by_EmojicBot")
        _LINK_EMOJI_ID = ss.stickers[-7].custom_emoji_id
        log.info("Link emoji loaded", emoji_id=_LINK_EMOJI_ID)
    except Exception as e:
        log.warning("Failed to fetch link emoji", error=str(e))


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
            f"解析 <code>{html.escape(url[:60])}</code> 时返回需要登录的错误。\n"
            f"{err_block}\n"
            f"建议先诊断：<code>/testcookie {platform.lower()}</code>\n"
            f"确认后更新 Cookie：<code>/setcookie {platform.lower()} &lt;新Cookie&gt;</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Failed to notify owner of cookie expiry", error=str(e))


# Per-platform cooldown for owner failure alerts: at most one per 6 hours so a
# misbehaving feed (or a missing cookie hit repeatedly) can't spam the owner.
_OWNER_ALERT_COOLDOWN = 6 * 3600
_last_owner_alert: dict[str, float] = {}


async def _alert_owner_media_failure(context, url: str, stage: str, err: str) -> None:
    """Push a throttled alert to the owner for a parse/download/send failure on a
    known cookie-bearing platform. No-ops for unknown platforms or if alerted
    recently for the same platform."""
    try:
        url_lower = url.lower()
        cookie_key = next(
            (k for d, k in _DOMAIN_COOKIE_KEY.items() if d in url_lower), None
        )
        if not cookie_key:
            return  # only platforms whose failures the owner can fix via cookies
        platform = cookie_key.replace("cookie_", "")

        now = time.monotonic()
        last = _last_owner_alert.get(platform, 0.0)
        if now - last < _OWNER_ALERT_COOLDOWN:
            return
        _last_owner_alert[platform] = now

        cfg = context.bot_data.get("config")
        owner_id = getattr(cfg, "owner_id", None)
        if not owner_id:
            return
        err_block = f"\n原始错误：<code>{html.escape(err[:200])}</code>\n" if err else ""
        await context.bot.send_message(
            owner_id,
            f"⚠️ <b>{platform} 媒体{stage}失败</b>\n\n"
            f"链接 <code>{html.escape(url[:60])}</code> 处理失败，多半是 Cookie 未配置或已过期。\n"
            f"{err_block}\n"
            f"详情见 Web Admin → 媒体错误日志。\n"
            f"诊断：<code>/testcookie {platform}</code>　更新：<code>/setcookie {platform} &lt;新Cookie&gt;</code>\n"
            f"<i>（同平台 6 小时内只提醒一次）</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Failed to alert owner of media failure", error=str(e))


# ---------------------------------------------------------------------------
# 小红书 (XHS) image-URL rewriting
# ---------------------------------------------------------------------------

def _xhs_trace_id(url: str) -> Optional[str]:
    """Extract the stable image trace id from an XHS CDN url.

    ``http://sns-webpic-qc.xhscdn.com/{ts}/{hash}/{traceId}!nd_dft_…`` → traceId.
    The timestamped ``sns-webpic`` urls expire / 403; the trace id rebuilt as
    ``ci.xiaohongshu.com/{traceId}`` serves the full image reliably.
    """
    if not url or "xhscdn.com" not in url:
        return None
    last = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    trace = last.split("!")[0].strip()
    return trace or None


def _rewrite_xhs_media(result) -> None:
    """In-place: point XHS image refs at the stable ci.xiaohongshu.com CDN."""
    try:
        from parsehub.types import ImageRef, LivePhotoRef
    except Exception:
        return
    media = getattr(result, "media", None)
    if media is None:
        return
    items = media if isinstance(media, (list, tuple)) else [media]
    for m in items:
        if not isinstance(m, (ImageRef, LivePhotoRef)):
            continue
        trace = _xhs_trace_id(getattr(m, "url", "") or "")
        if trace:
            m.url = f"https://ci.xiaohongshu.com/{trace}"


# ---------------------------------------------------------------------------
# Download-result helpers
# ---------------------------------------------------------------------------

def _get_files(dr) -> list[Path]:
    """Collect existing local file paths from a ParseHub DownloadResult."""
    media = getattr(dr, "media", None)
    if media is None:
        return []
    items = media if isinstance(media, (list, tuple)) else [media]
    paths: list[Path] = []
    for m in items:
        p = getattr(m, "path", None)
        if p:
            paths.append(Path(p))
        vp = getattr(m, "video_path", None)   # LivePhotoFile carries both
        if vp:
            paths.append(Path(vp))
    return [p for p in paths if p.exists()]


def _make_caption(
    title: str,
    content: str,
    url: str,
    bot_username: str = "",
    *,
    limit: int = 1024,
    extra: str = "",
) -> str:
    """Build an HTML caption that always fits within ``limit`` characters and is
    always well-formed (the ``<blockquote>`` is opened and closed together, never
    sliced)."""
    link_icon = (
        f'<tg-emoji emoji-id="{_LINK_EMOJI_ID}">🔗</tg-emoji>'
        if _LINK_EMOJI_ID else "🔗"
    )
    via = f" | Share Via @{bot_username}" if bot_username else ""
    footer = f'\n{link_icon} <a href="{html.escape(url, quote=True)}">Source</a>{via}'
    tail = footer + (extra or "")

    title = (title or "").strip()
    head = f"<b>{html.escape(title[:200])}</b>\n" if title else ""

    block = ""
    content = (content or "").strip()
    if content:
        expandable = len(content) > 300
        open_tag = "<blockquote expandable>" if expandable else ""
        close_tag = "</blockquote>" if expandable else ""
        room = limit - len(head) - len(tail) - len(open_tag) - len(close_tag) - 1
        if room > 20:
            # Truncate raw text first, then escape — escaping after the cut avoids
            # splitting a multi-char entity (e.g. "&amp;"). Shrink until it fits.
            snippet = content[:room]
            esc = html.escape(snippet)
            while esc and len(esc) > room:
                snippet = snippet[: max(1, int(len(snippet) * room / len(esc)) - 1)]
                esc = html.escape(snippet)
            if esc:
                block = f"{open_tag}{esc}{close_tag}\n"

    return f"{head}{block}{tail}".strip()


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Best-effort fallback: drop tags + unescape entities for plain-text resend."""
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _is_entity_error(err: Exception) -> bool:
    m = str(err).lower()
    return "parse entities" in m or "end tag" in m or "can't parse" in m


def _is_dimension_error(err: Exception) -> bool:
    # Telegram rejects photos with width+height >= 10000 or aspect ratio > 20
    # ("Photo_invalid_dimensions" / "PHOTO_INVALID_DIMENSIONS").
    return "dimension" in str(err).lower()


def _fit_image_dims(fp: Path) -> Path:
    """Return a path to a version of the image that satisfies Telegram's photo
    dimension limits (width+height < 10000), downscaling if necessary.

    Uniform scaling can only fix the size-sum limit, not an extreme aspect ratio
    (> 20) — those are left for the send-as-document fallback. Best-effort: any
    failure (including Pillow being unavailable) returns the original path."""
    try:
        from PIL import Image

        with Image.open(fp) as im:
            w, h = im.size
            if w <= 0 or h <= 0 or w + h < 10000:
                return fp
            if max(w, h) / min(w, h) > 20:
                return fp  # too elongated to rescue by scaling → document route
            scale = 9990 / (w + h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            resized = im.resize((nw, nh), Image.LANCZOS)
            if fp.suffix.lower() in (".jpg", ".jpeg") and resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            out = fp.with_name(f"{fp.stem}_tg{fp.suffix}")
            resized.save(out)
            log.info("Downscaled oversized image for Telegram",
                     file=fp.name, frm=f"{w}x{h}", to=f"{nw}x{nh}")
            return out
    except Exception as e:
        log.warning("Image resize for Telegram dims failed", file=fp.name, error=str(e))
        return fp


# ---------------------------------------------------------------------------
# Robust senders — retry once as plain text if HTML entity parsing fails,
# reopening files so consumed handles don't break the retry.
# ---------------------------------------------------------------------------

async def _send_one(bot, chat_id: int, fp: Path, caption: str, reply_to: int) -> None:
    img_exts   = {".jpg", ".jpeg", ".png", ".webp"}
    video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
    audio_exts = {".mp3", ".m4a", ".ogg", ".opus", ".flac"}
    ext = fp.suffix.lower()
    # Pre-shrink oversized images so they clear Telegram's dimension check.
    send_fp = _fit_image_dims(fp) if ext in img_exts else fp

    async def _send_as_document(cap: str, parse_mode):
        with open(send_fp, "rb") as f:
            await bot.send_document(chat_id, document=f, caption=cap,
                                    parse_mode=parse_mode, reply_to_message_id=reply_to)

    async def _do(cap: str, parse_mode):
        with open(send_fp, "rb") as f:
            if ext in img_exts:
                try:
                    await bot.send_photo(chat_id, photo=f, caption=cap,
                                         parse_mode=parse_mode, reply_to_message_id=reply_to)
                except BadRequest as e:
                    if _is_dimension_error(e):
                        # Extreme aspect ratio — send full-quality as a document.
                        await _send_as_document(cap, parse_mode)
                    else:
                        raise
            elif ext in video_exts:
                await bot.send_video(chat_id, video=f, caption=cap, parse_mode=parse_mode,
                                     supports_streaming=True, reply_to_message_id=reply_to)
            elif ext in audio_exts:
                await bot.send_audio(chat_id, audio=f, caption=cap,
                                     parse_mode=parse_mode, reply_to_message_id=reply_to)
            elif ext == ".gif":
                await bot.send_animation(chat_id, animation=f, caption=cap,
                                         parse_mode=parse_mode, reply_to_message_id=reply_to)
            else:
                await bot.send_document(chat_id, document=f, caption=cap,
                                        parse_mode=parse_mode, reply_to_message_id=reply_to)

    try:
        await _do(caption, ParseMode.HTML)
    except BadRequest as e:
        if _is_entity_error(e):
            await _do(_strip_html(caption), None)
        else:
            raise


async def _send_group(bot, chat_id: int, files: list[Path], caption: str, reply_to: int) -> None:
    img_exts = {".jpg", ".jpeg", ".png", ".webp"}
    # Pre-shrink oversized images; non-images pass through unchanged.
    send_files = [
        _fit_image_dims(fp) if fp.suffix.lower() in img_exts else fp for fp in files
    ]

    async def _do(cap: str, parse_mode):
        fhs = [open(fp, "rb") for fp in send_files]
        try:
            group = []
            for i, (fp, fh) in enumerate(zip(send_files, fhs)):
                c = cap if i == 0 else ""
                if fp.suffix.lower() in img_exts:
                    group.append(InputMediaPhoto(media=fh, caption=c, parse_mode=parse_mode))
                else:
                    group.append(InputMediaVideo(media=fh, caption=c, parse_mode=parse_mode,
                                                  supports_streaming=True))
            await bot.send_media_group(chat_id, media=group, reply_to_message_id=reply_to)
        finally:
            for fh in fhs:
                fh.close()

    async def _send_individually() -> None:
        # An album is all-or-nothing; if one item is rejected for its dimensions,
        # fall back to sending each file on its own (_send_one handles the
        # per-image photo→document fallback). Caption rides on the first file.
        for i, fp in enumerate(files):
            await _send_one(bot, chat_id, fp, caption if i == 0 else "", reply_to)

    try:
        await _do(caption, ParseMode.HTML)
    except BadRequest as e:
        if _is_entity_error(e):
            try:
                await _do(_strip_html(caption), None)
            except BadRequest as e2:
                if _is_dimension_error(e2):
                    await _send_individually()
                else:
                    raise
        elif _is_dimension_error(e):
            await _send_individually()
        else:
            raise


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def _process_url(update: Update, context, url: str, delete_original: bool = True) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    status = await msg.reply_text("⏳ 解析中…")
    tmp_dir = tempfile.mkdtemp(prefix="ponygram_")

    is_weixin  = "mp.weixin.qq.com" in url.lower()
    is_xhs     = any(d in url.lower() for d in ("xiaohongshu.com", "xhslink.com"))
    is_bili    = any(d in url.lower() for d in ("bilibili.com", "b23.tv"))
    is_youtube = any(d in url.lower() for d in ("youtube.com", "youtu.be"))

    bot_username = context.bot.username or ""

    async def _delete_original() -> None:
        if not delete_original:
            return
        try:
            await msg.delete()
        except TelegramError:
            pass

    try:
        cookie = await _get_cookie(url)
        proxy = _get_proxy()

        try:
            from parsehub import ParseHub
            from parsehub.errors import UnknownPlatform
            _patch_parsehub()
            ph = ParseHub()
        except Exception as e:
            log.warning("ParseHub unavailable", error=str(e))
            await status.delete()
            return

        # Bilibili: hand the cookie to our get_video_info shim (bypasses 412).
        if is_bili and cookie:
            _bili_cookie_ctx.set(_parse_cookie_str(cookie))

        # YouTube is the exception: feeding yt-dlp a cookie on a datacenter/WARP
        # IP makes YouTube reject the session ("Requested format is not
        # available"), whereas the anonymous android client works. So never pass
        # the YouTube cookie — the player_client patch handles reliability.
        parse_cookie = None if is_youtube else cookie

        # ------------------------------------------------------------------ #
        # Parse                                                               #
        # ------------------------------------------------------------------ #
        try:
            result = await asyncio.wait_for(
                ph.parse(url, proxy=proxy, cookie=parse_cookie),
                timeout=45,
            )
        except UnknownPlatform:
            await status.delete()
            return
        except Exception as e:
            err = str(e)
            log.warning(
                "ParseHub parse failed",
                url=url, error=err, err_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )
            if _is_auth_error(err) and parse_cookie:
                await _notify_cookie_expired(context, url, err)
            await _fail_silently(status, url, "parse", e, chat.id, context=context)
            return

        # ------------------------------------------------------------------ #
        # WeChat: post full article to Telegraph                              #
        # ------------------------------------------------------------------ #
        if is_weixin:
            art_title = (getattr(result, "title", "") or "").strip()
            md = (
                getattr(result, "markdown_content", "")
                or getattr(result, "content", "")
                or ""
            ).strip()
            token = await _ensure_telegraph_token()
            if token and md:
                try:
                    nodes = _md_to_telegraph_nodes(md)
                    tg_url = await _post_to_telegraph(art_title, nodes, token)
                    link_icon = (
                        f'<tg-emoji emoji-id="{_LINK_EMOJI_ID}">🔗</tg-emoji>'
                        if _LINK_EMOJI_ID else "🔗"
                    )
                    preview = _first_paragraph(md)
                    preview_line = f"\n{html.escape(preview)}" if preview else ""
                    via = f" | Share Via @{bot_username}" if bot_username else ""
                    await status.delete()
                    status = None
                    await context.bot.send_message(
                        chat.id,
                        text=(
                            f"<b>{html.escape(art_title)}</b>{preview_line}\n\n"
                            f'{link_icon} <a href="{html.escape(tg_url, quote=True)}">阅读全文</a>{via}'
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                    await _delete_original()
                    return
                except Exception as e:
                    log.warning("Telegraph post failed, falling through", error=str(e))
                    if status is None:
                        status = await msg.reply_text("⏳ 解析中…")

        # ------------------------------------------------------------------ #
        # Download (ParseHub-native; XHS needs URL rewrite + headers)         #
        # ------------------------------------------------------------------ #
        platform_name = (
            getattr(getattr(result, "platform", None), "display_name", "") or "Media"
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
        download_err: Optional[Exception] = None
        try:
            if is_xhs:
                _rewrite_xhs_media(result)
                dr = await asyncio.wait_for(
                    result._do_download(output_dir=tmp_dir, proxy=proxy, headers=_XHS_HEADERS),
                    timeout=240,
                )
            else:
                dr = await asyncio.wait_for(
                    result.download(path=tmp_dir, proxy=proxy),
                    timeout=240,
                )
            files = _get_files(dr)
        except Exception as e:
            download_err = e
            log.warning("ParseHub download failed", url=url,
                        error=str(e), err_type=type(e).__name__)

        title   = (getattr(result, "title",   "") or "").strip()
        content = (getattr(result, "content", "") or "").strip()
        caption = _make_caption(title, content, url, bot_username)

        if not files:
            if download_err and _is_auth_error(str(download_err)) and parse_cookie:
                await _notify_cookie_expired(context, url, str(download_err))
            await _fail_silently(
                status, url, "download",
                download_err or "下载失败：未获得任何文件",
                chat.id, context=context,
            )
            return

        # Drop anything over the Telegram upload ceiling — sending it would just
        # bounce with a cryptic error. Record what we skipped.
        sendable: list[Path] = []
        for fp in files:
            size_mb = fp.stat().st_size / 1024 / 1024
            if size_mb > _DL_MAX_MB:
                log.warning("Skipping oversized media", file=fp.name,
                            size_mb=round(size_mb, 1), limit_mb=_DL_MAX_MB)
            else:
                sendable.append(fp)

        if not sendable:
            await _fail_silently(
                status, url, "download",
                f"文件超出 Telegram 上限 ({_DL_MAX_MB} MB)，未发送",
                chat.id,
            )
            return

        if status:
            await status.delete()
            status = None

        await context.bot.send_chat_action(chat.id, chat_action)

        img_exts   = {".jpg", ".jpeg", ".png", ".webp"}
        video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi"}

        group_items = [f for f in sendable if f.suffix.lower() in img_exts | video_exts]
        singles     = [f for f in sendable if f.suffix.lower() not in img_exts | video_exts]

        files_sent = 0
        if len(group_items) >= 2:
            await _send_group(context.bot, chat.id, group_items[:10], caption, msg.message_id)
            files_sent += len(group_items[:10])
        elif len(group_items) == 1:
            await _send_one(context.bot, chat.id, group_items[0], caption, msg.message_id)
            files_sent += 1

        first_single = files_sent == 0
        for fp in singles[: 10 - files_sent]:
            await _send_one(context.bot, chat.id, fp,
                            caption if first_single else "", msg.message_id)
            first_single = False
            files_sent += 1

        log.info("Media sent", platform=platform_name, chat_id=chat.id, files=files_sent)
        await _delete_original()

    except TelegramError as e:
        log.warning("Media send failed", error=str(e))
        await _fail_silently(status, url, "send", e, chat.id, context=context)
    except Exception as e:
        log.warning("Media pipeline error", error=str(e), traceback=traceback.format_exc())
        await _fail_silently(status, url, "pipeline", e, chat.id)
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

    delete_original = True
    if chat.type in ("group", "supergroup"):
        settings = await get_group_settings(chat.id)
        if not settings.is_active:
            return
        if not settings.dlmode_enabled:
            return
        delete_original = settings.media_del_original

    match = _URL_RE.search(msg.text)
    if not match:
        return

    url = match.group(0).rstrip(".,;:!?)'\"")
    if not _is_known_url(url):
        return

    # Module disabled in 组件中心 → ignore the link silently.
    if not await is_module_enabled("media"):
        return

    await _process_url(update, context, url, delete_original=delete_original)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def _cookie_field(key: str, name: str, required: bool) -> ConfigField:
    tag = "需要登录 Cookie" if required else "公开内容无需 Cookie，私密内容才需要"
    return ConfigField(
        key=key,
        label=f"{name} Cookie",
        kind="cookie",
        help=f"{tag}。粘贴浏览器导出的 Cookie 字符串，保存时自动规范化。",
    )


_COMPONENT = Component(
    id="media",
    name="媒体解析",
    icon="bi-play-btn",
    description="自动检测消息中的视频/图文链接并下载转发。各平台登录 Cookie 直接在此配置；"
                "更丰富的按平台测试可前往「媒体解析」页面。",
    config_keys=[
        _cookie_field("cookie_bilibili",    "哔哩哔哩",   required=True),
        _cookie_field("cookie_youtube",     "YouTube",    required=False),
        _cookie_field("cookie_instagram",   "Instagram",  required=True),
        _cookie_field("cookie_twitter",     "Twitter / X", required=True),
        _cookie_field("cookie_tiktok",      "TikTok",     required=False),
        _cookie_field("cookie_douyin",      "抖音",       required=False),
        _cookie_field("cookie_xiaohongshu", "小红书",     required=False),
        _cookie_field("cookie_kuaishou",    "快手",       required=False),
    ],
    order=20,
)


def setup(application: Application) -> None:
    register_component(_COMPONENT)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
            on_message_url,
        ),
        group=20,
    )
    application.job_queue.run_once(_init_link_emoji, when=0)
    log.info("media module loaded")
