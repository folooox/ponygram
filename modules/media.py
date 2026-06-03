"""
Media URL parsing & download module — powered by ParseHub.

Supports 17+ platforms: YouTube, Bilibili, TikTok/Douyin, Instagram,
Twitter/X, 微博, 小红书, 贴吧, Threads, Facebook, 快手, and more.

Trigger: paste any URL → bot sends "⏳ 解析中…" → sends media on success;
silently does nothing for unsupported platforms or failures.

Only active groups (is_active=True) get URL processing.
Private chats always get URL processing.
Per-group dlmode_enabled toggle (default True) controls whether parsing runs.
Per-group media_del_original toggle (default True) controls whether the user's
original link message is deleted after the parsed media is sent.
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

import aiohttp
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters

from bot.components import Component, ConfigField, is_module_enabled, register_component
from bot.database import get_group_settings
from bot.logger import get_logger
from bot.telegraph import (
    _ensure_telegraph_token,
    _first_paragraph,
    _md_to_telegraph_nodes,
    _parse_inline_md,
    _post_to_telegraph,
)

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
    "mp.weixin.qq.com",
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

_LINK_EMOJI_ID: str | None = None

_XHS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
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


def _bili_parse_cookie(cookie_str: str) -> dict:
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
    Download a Bilibili video (BVID-based) at 720p using stored cookie.
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

        # Resolve short links (b23.tv)
        if any(x in url.lower() for x in ("b23.tv", "bili2233.cn")):
            resp = await client.get(url)
            url = str(resp.url)
            log.info("Bili short link resolved", final=url)

        # Extract BVID — raises ValueError if not found (caller falls back to parsehub)
        m = re.search(r"BV[0-9A-Za-z]{10,}", url)
        if not m:
            raise ValueError(f"No BVID in URL: {url}")
        bvid = m.group(0)

        # Step 1: video metadata
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
        log.info("Bili video info ok", bvid=bvid, cid=cid, title=title[:40])

        # Step 2: buvid fingerprint
        r2 = await client.get("https://api.bilibili.com/x/frontend/finger/spi")
        spi = r2.json().get("data", {})
        full_cookies = {
            **cookies,
            "buvid3": spi.get("b_3", ""),
            "buvid4": spi.get("b_4", ""),
        }

        # Step 3: playurl at 720p (qn=64)
        r3 = await client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": 64,
                "fnver": 0,
                "fnval": 1,
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
                f"playurl 返回空 durl: code={pjson.get('code')} "
                f"msg={pjson.get('message')} quality={pdata.get('quality')}"
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

        # Step 4: stream download
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
# XHS direct downloader (images + video)
# ---------------------------------------------------------------------------

async def _xhs_download_media(
    result,
    cookie: Optional[str],
    proxy: Optional[str],
    tmp_dir: str,
) -> list[Path]:
    """
    Download all media (images OR video) from a parsed XHS result.

    parsehub's Media class stores the remote URL in `.path`, not `.url`.
    For video posts parsehub returns a single Video item; for image posts
    it returns a list of Image items.  Both need Referer + optional cookie
    for the XHS CDN to serve the real content.
    """
    import httpx

    try:
        from parsehub.types.media import Video as _PHVideo
    except ImportError:
        _PHVideo = None

    media = getattr(result, "media", None)
    if not media:
        return []
    items = media if isinstance(media, (list, tuple)) else [media]

    headers = dict(_XHS_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    files: list[Path] = []

    # Separate timeout for video (larger files, longer read window)
    img_timeout   = httpx.Timeout(15.0, read=60.0)
    video_timeout = httpx.Timeout(20.0, read=300.0)

    async with httpx.AsyncClient(
        headers=headers,
        proxy=proxy,
        follow_redirects=True,
    ) as client:
        for i, m in enumerate(items):
            # parsehub stores the CDN URL in .path; .url does not exist
            media_url = getattr(m, "path", None)
            if not media_url or not str(media_url).startswith("http"):
                continue

            is_video = _PHVideo is not None and isinstance(m, _PHVideo)
            timeout  = video_timeout if is_video else img_timeout

            try:
                async with client.stream("GET", str(media_url), timeout=timeout) as resp:
                    resp.raise_for_status()
                    ct = resp.headers.get("content-type", "")

                    if is_video or "video" in ct or "mp4" in ct:
                        ext = ".mp4"
                    elif "webp" in ct:
                        ext = ".webp"
                    elif "png" in ct:
                        ext = ".png"
                    elif "gif" in ct:
                        ext = ".gif"
                    else:
                        ext = ".jpg"

                    out = Path(tmp_dir) / f"xhs_{i:03d}{ext}"
                    with open(out, "wb") as f:
                        async for chunk in resp.aiter_bytes(131072):
                            f.write(chunk)

                size_mb = out.stat().st_size / 1024 / 1024
                log.debug("XHS media downloaded", index=i, ext=ext,
                          size_mb=round(size_mb, 1))
                files.append(out)
            except Exception as e:
                log.warning("XHS media download failed", index=i,
                            url=str(media_url)[:80], error=str(e))

    log.info("XHS direct download", total=len(items), saved=len(files))
    return files


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


def _get_files(dr, skip_video_path: bool = False) -> list[Path]:
    media = getattr(dr, "media", None)
    if media is None:
        return []
    items = media if isinstance(media, (list, tuple)) else [media]
    paths: list[Path] = []
    for m in items:
        p = getattr(m, "path", None)
        if p:
            paths.append(Path(p))
        if not skip_video_path:
            vp = getattr(m, "video_path", None)
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
    """Build an HTML caption that always fits within ``limit`` characters.

    Title and content are HTML-escaped, and the variable-length content block is
    trimmed so the closing ``</blockquote>`` tag, the ``Source`` link and the
    ``Share Via @bot`` footer are never sliced off (which corrupts the HTML and
    makes Telegram reject the message with "can't find end tag …").
    """
    link_icon = (
        f'<tg-emoji emoji-id="{_LINK_EMOJI_ID}">🔗</tg-emoji>'
        if _LINK_EMOJI_ID else "🔗"
    )
    via = f" | Share Via @{bot_username}" if bot_username else ""
    footer = f'\n{link_icon} <a href="{url}">Source</a>{via}'
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
            # Truncate the raw text first, then escape — escaping after the cut
            # avoids splitting a multi-char entity (e.g. "&amp;"). Shrink until
            # the escaped result fits the remaining room.
            snippet = content[:room]
            esc = html.escape(snippet)
            while esc and len(esc) > room:
                snippet = snippet[: max(1, int(len(snippet) * room / len(esc)) - 1)]
                esc = html.escape(snippet)
            if esc:
                block = f"{open_tag}{esc}{close_tag}\n"

    return f"{head}{block}{tail}".strip()


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def _process_url(update: Update, context, url: str, delete_original: bool = True) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    status = await msg.reply_text("⏳ 解析中…")
    tmp_dir = tempfile.mkdtemp(prefix="ponygram_")

    is_weixin = "mp.weixin.qq.com" in url.lower()
    is_xhs    = any(d in url.lower() for d in ("xiaohongshu.com", "xhslink.com"))
    is_bili   = any(d in url.lower() for d in ("bilibili.com", "b23.tv"))

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
        proxy  = _get_proxy()

        # ------------------------------------------------------------------ #
        # Bilibili direct path (BVID + cookie required)                       #
        # ------------------------------------------------------------------ #
        if is_bili and cookie:
            try:
                await status.edit_text("⏳ 解析中… (Bilibili)")
                await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)
                video_path, bili_title, bili_thumb = await asyncio.wait_for(
                    _bili_direct_parse(url, cookie, proxy, tmp_dir),
                    timeout=300,
                )
                caption = _make_caption(bili_title, "", url, bot_username)
                await status.delete()
                status = None
                with open(video_path, "rb") as f:
                    await context.bot.send_video(
                        chat.id,
                        video=f,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        reply_to_message_id=msg.message_id,
                    )
                log.info("Bili direct sent", chat_id=chat.id)
                await _delete_original()
                return
            except Exception as e:
                log.warning("Bili direct parse failed, falling back to parsehub", error=str(e))
                if status is None:
                    status = await msg.reply_text("⏳ 解析中…")

        # ------------------------------------------------------------------ #
        # Generic parsehub path                                               #
        # ------------------------------------------------------------------ #
        try:
            from parsehub import ParseHub
            from parsehub.errors import UnknownPlatform
            ph = ParseHub()
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
        except Exception as e:
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

        # ------------------------------------------------------------------ #
        # WeChat: post full article to Telegraph                              #
        # ------------------------------------------------------------------ #
        if is_weixin:
            art_title = (getattr(result, "title", "") or "").strip()
            md = (getattr(result, "markdown_content", "") or getattr(result, "content", "") or "").strip()
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
                        text=f"<b>{html.escape(art_title)}</b>{preview_line}\n\n{link_icon} <a href=\"{tg_url}\">阅读全文</a>{via}",
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                    await _delete_original()
                    return
                except Exception as e:
                    log.warning("Telegraph post failed, falling through", error=str(e))

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
        if is_xhs:
            # XHS CDN requires Referer + cookie; download media directly.
            files = await _xhs_download_media(result, cookie, proxy, tmp_dir)
            if not files:
                # Fallback: let parsehub try its own downloader
                try:
                    dr = await asyncio.wait_for(
                        result.download(path=tmp_dir, proxies=proxy),
                        timeout=180,
                    )
                    files = _get_files(dr, skip_video_path=False)
                except (asyncio.TimeoutError, Exception) as e:
                    log.warning("XHS parsehub fallback download also failed", url=url, error=str(e))
        else:
            try:
                dr = await asyncio.wait_for(
                    result.download(path=tmp_dir, proxy=proxy),
                    timeout=180,
                )
                files = _get_files(dr, skip_video_path=False)
            except (asyncio.TimeoutError, Exception) as e:
                log.warning("ParseHub download failed", url=url, error=str(e))

        title   = (getattr(result, "title",   "") or "").strip()
        content = (getattr(result, "content", "") or "").strip()
        caption = _make_caption(title, content, url, bot_username)

        async def _send_info_card(extra: str = "") -> None:
            thumbnail = _get_thumbnail(result)
            if thumbnail:
                try:
                    await context.bot.send_photo(
                        chat.id,
                        photo=thumbnail,
                        caption=_make_caption(title, content, url, bot_username, extra=extra),
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                    return
                except TelegramError:
                    pass
            await context.bot.send_message(
                chat.id,
                text=_make_caption(title, content, url, bot_username, limit=4096, extra=extra),
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

        if status:
            await status.delete()
            status = None

        await context.bot.send_chat_action(chat.id, chat_action)

        _img_exts   = {".jpg", ".jpeg", ".png", ".webp"}
        _video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
        _audio_exts = {".mp3", ".m4a", ".ogg", ".opus", ".flac"}

        group_items = [f for f in files if f.suffix.lower() in _img_exts | _video_exts]
        singles     = [f for f in files if f.suffix.lower() not in _img_exts | _video_exts]

        files_sent = 0

        if len(group_items) >= 2:
            fhs = [open(fp, "rb") for fp in group_items[:10]]
            try:
                media_group = []
                for i, (fp, fh) in enumerate(zip(group_items[:10], fhs)):
                    c = caption if i == 0 else ""
                    if fp.suffix.lower() in _img_exts:
                        media_group.append(InputMediaPhoto(
                            media=fh, caption=c, parse_mode=ParseMode.HTML,
                        ))
                    else:
                        media_group.append(InputMediaVideo(
                            media=fh, caption=c, parse_mode=ParseMode.HTML,
                            supports_streaming=True,
                        ))
                await context.bot.send_media_group(
                    chat.id, media=media_group, reply_to_message_id=msg.message_id,
                )
                files_sent += len(fhs)
            finally:
                for fh in fhs:
                    fh.close()
        elif len(group_items) == 1:
            fp = group_items[0]
            with open(fp, "rb") as f:
                if fp.suffix.lower() in _img_exts:
                    await context.bot.send_photo(
                        chat.id, photo=f, caption=caption,
                        parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id,
                    )
                else:
                    await context.bot.send_video(
                        chat.id, video=f, caption=caption,
                        parse_mode=ParseMode.HTML, supports_streaming=True,
                        reply_to_message_id=msg.message_id,
                    )
            files_sent += 1

        first_single = files_sent == 0
        for fp in singles[:10 - files_sent]:
            ext = fp.suffix.lower()
            c = caption if first_single else ""
            first_single = False
            with open(fp, "rb") as f:
                if ext in _audio_exts:
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
        await _delete_original()

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
