"""
Music search module — powered by Last.fm API.

Accessible via:
  @bot music <artist - track>  — inline picker
  @bot artist <name>           — inline picker
  /music <query>               — selection-first command (album art)
  /artist <name>               — direct command (single result)

Requires lastfm_api_key in Web Admin → Settings.
Results cached 30 minutes per query.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import music_cache
from bot.database import get_bot_config
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

_LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


async def _get_api_key(context) -> Optional[str]:
    key = await get_bot_config("lastfm_api_key")
    if key:
        return key
    cfg = context.bot_data.get("config") if context else None
    return getattr(cfg, "lastfm_api_key", None) if cfg else None


async def _lastfm(method: str, api_key: str, **params) -> Optional[Dict]:
    params.update({"method": method, "api_key": api_key, "format": "json"})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_LASTFM_BASE, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                if "error" in data:
                    log.warning("Last.fm error", code=data["error"], message=data.get("message"))
                    return None
                return data
    except Exception as e:
        log.warning("Last.fm request failed", error=str(e))
        return None


def _playcount_fmt(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _strip_html(text: str) -> str:
    text = re.sub(r'<a href="[^"]*">.*?</a>', "", text).strip()
    return re.sub(r"<[^>]+>", "", text)


def get_track_image(detail: Dict) -> Optional[str]:
    album = detail.get("album") or {}
    images = album.get("image") or []
    for size in ("extralarge", "large", "medium"):
        for img in images:
            url = img.get("#text", "")
            if img.get("size") == size and url:
                return url
    return None


def get_artist_image(artist: Dict) -> Optional[str]:
    images = artist.get("image") or []
    for size in ("extralarge", "large", "mega"):
        for img in images:
            url = img.get("#text", "")
            if img.get("size") == size and url:
                return url
    return None


def _format_track(track: Dict) -> str:
    name = track.get("name", "Unknown")
    artist = track.get("artist", {})
    artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist)
    listeners = track.get("listeners", "")
    playcount = track.get("playcount", "")
    url = track.get("url", "")

    wiki = track.get("wiki") or {}
    summary = _strip_html(wiki.get("summary", ""))
    if len(summary) > 300:
        summary = summary[:300].rstrip() + "…"

    tags = track.get("toptags", {}).get("tag", [])
    tag_names = ", ".join(t["name"] for t in tags[:5]) if tags else ""

    lines = [f"🎵 <b>{name}</b>"]
    if artist_name:
        lines.append(f"👤 {artist_name}")
    if listeners:
        lines.append(f"👥 {_playcount_fmt(listeners)} listeners · 🔄 {_playcount_fmt(playcount)} plays")
    if tag_names:
        lines.append(f"🏷 {tag_names}")
    if summary:
        lines.append(f"\n{summary}")
    if url:
        lines.append(f'\n🔗 <a href="{url}">Last.fm</a>')
    return "\n".join(lines)


def _format_artist(artist: Dict, top_tracks: List[Dict]) -> str:
    name = artist.get("name", "Unknown")
    listeners = artist.get("stats", {}).get("listeners", "")
    playcount = artist.get("stats", {}).get("playcount", "")
    url = artist.get("url", "")

    bio = artist.get("bio") or {}
    summary = _strip_html(bio.get("summary", ""))
    if len(summary) > 300:
        summary = summary[:300].rstrip() + "…"

    tags = artist.get("tags", {}).get("tag", [])
    tag_names = ", ".join(t["name"] for t in tags[:5]) if tags else ""

    lines = [f"🎤 <b>{name}</b>"]
    if listeners:
        lines.append(f"👥 {_playcount_fmt(listeners)} listeners · 🔄 {_playcount_fmt(playcount)} plays")
    if tag_names:
        lines.append(f"🏷 {tag_names}")
    if summary:
        lines.append(f"\n{summary}")
    if top_tracks:
        lines.append("\n🎵 <b>Top tracks:</b>")
        for i, t in enumerate(top_tracks[:5], 1):
            t_name = t.get("name", "?")
            t_url = t.get("url", "")
            lines.append(f'{i}. <a href="{t_url}">{t_name}</a>' if t_url else f"{i}. {t_name}")
    if url:
        lines.append(f'\n🔗 <a href="{url}">Last.fm</a>')
    return "\n".join(lines)


async def search_tracks(query: str, context) -> Optional[list]:
    api_key = await _get_api_key(context)
    if not api_key:
        return None
    cache_key = f"track:{query.lower()}"
    cached = await music_cache.get(cache_key)
    if cached:
        return cached
    artist_q, track_q = "", query
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_q, track_q = parts[0].strip(), parts[1].strip()
    params: dict = {"track": track_q, "limit": 5}
    if artist_q:
        params["artist"] = artist_q
    data = await _lastfm("track.search", api_key, **params)
    if not data:
        return None
    results = data.get("results", {}).get("trackmatches", {}).get("track", [])
    if results:
        await music_cache.set(cache_key, results)
    return results


async def search_artist(name: str, context) -> Optional[tuple]:
    api_key = await _get_api_key(context)
    if not api_key:
        return None
    cache_key = f"artist:{name.lower()}"
    cached = await music_cache.get(cache_key)
    if cached:
        return cached
    artist_data = await _lastfm("artist.getInfo", api_key, artist=name)
    if not artist_data:
        return None
    artist = artist_data.get("artist", {})
    if not artist:
        return None
    top_data = await _lastfm("artist.getTopTracks", api_key, artist=name, limit=5)
    top_tracks = (top_data or {}).get("toptracks", {}).get("track", [])
    result = (artist, top_tracks)
    await music_cache.set(cache_key, result)
    return result


async def _fetch_track_detail(api_key: str, artist_name: str, track_name: str) -> Dict:
    detail_key = f"track_info:{artist_name}:{track_name}".lower()
    cached = await music_cache.get(detail_key)
    if cached:
        return cached
    d = await _lastfm("track.getInfo", api_key, artist=artist_name, track=track_name)
    detail = d.get("track", {}) if d else {}
    if detail:
        await music_cache.set(detail_key, detail)
    return detail


def _music_select_keyboard(results: list, cache_key: str) -> InlineKeyboardMarkup:
    rows: list = []
    row: list = []
    for i, t in enumerate(results[:5]):
        a = t.get("artist", "?")
        artist_name = a.get("name", "?") if isinstance(a, dict) else str(a)
        name = t.get("name", "?")[:16]
        label = f"{i+1}. {name} — {artist_name[:12]}"
        row.append(InlineKeyboardButton(label, callback_data=f"music_sel:{cache_key}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def on_music_select(update: Update, context) -> None:
    q = update.callback_query
    assert q
    await q.answer()

    _, cache_key, idx_str = q.data.split(":", 2)
    idx = int(idx_str)

    results = await music_cache.get(cache_key)
    if not results or idx >= len(results):
        await q.edit_message_text("结果已过期，请重新搜索。")
        return

    await q.edit_message_text("⏳ 加载中…", reply_markup=None)

    top = results[idx]
    api_key = await _get_api_key(context)
    artist_name = (top.get("artist") or {})
    artist_name = artist_name.get("name", "") if isinstance(artist_name, dict) else str(artist_name)
    detail = {}
    if api_key:
        detail = await _fetch_track_detail(api_key, artist_name, top.get("name", ""))

    text = _format_track(detail or top)
    art_url = get_track_image(detail) if detail else None
    chat_id = q.message.chat_id if q.message else None

    try:
        await q.message.delete()
    except Exception:
        pass

    if art_url and chat_id:
        try:
            await context.bot.send_photo(
                chat_id, art_url,
                caption=text[:1024], parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass
    if chat_id:
        await context.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )


async def cmd_music(update: Update, context) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("用法：/music <歌手 - 歌曲名>  或  /music <歌曲名>")
        return

    query = " ".join(context.args)
    status = await msg.reply_text("🔍 搜索中…")
    results = await search_tracks(query, context)
    await status.delete()

    if results is None:
        await msg.reply_text("❌ Last.fm API 未配置，请在 Web 面板 → 设置 中添加 API Key")
        return
    if not results:
        await msg.reply_text(f"❌ 未找到：{query}")
        return

    short_key = hashlib.md5(f"track:{query.lower()}".encode()).hexdigest()[:16]
    await music_cache.set(short_key, results)

    if len(results) == 1:
        top = results[0]
        api_key = await _get_api_key(context)
        artist_name = (top.get("artist") or {})
        artist_name = artist_name.get("name", "") if isinstance(artist_name, dict) else str(artist_name)
        detail = {}
        if api_key:
            detail = await _fetch_track_detail(api_key, artist_name, top.get("name", ""))
        text = _format_track(detail or top)
        art_url = get_track_image(detail) if detail else None
        if art_url:
            try:
                await msg.reply_photo(art_url, caption=text[:1024], parse_mode=ParseMode.HTML)
                return
            except Exception:
                pass
        await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    else:
        lines = [f"🎵 搜到 <b>{len(results)}</b> 首歌曲，请选择：\n"]
        for i, t in enumerate(results[:5]):
            name = t.get("name", "?")
            a = t.get("artist", "?")
            artist_name = a.get("name", "?") if isinstance(a, dict) else str(a)
            lines.append(f"{i+1}. <b>{name}</b> — {artist_name}")
        keyboard = _music_select_keyboard(results, short_key)
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cmd_artist(update: Update, context) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("用法：/artist <歌手名>")
        return

    name = " ".join(context.args)
    status = await msg.reply_text("🔍 搜索中…")
    result = await search_artist(name, context)
    await status.delete()

    if result is None:
        await msg.reply_text("❌ Last.fm API 未配置，请在 Web 面板 → 设置 中添加 API Key")
        return
    artist, top_tracks = result
    if not artist:
        await msg.reply_text(f"❌ 未找到歌手：{name}")
        return

    text = _format_artist(artist, top_tracks)
    art_url = get_artist_image(artist)

    if art_url:
        try:
            await msg.reply_photo(art_url, caption=text[:1024], parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def setup(application: Application) -> None:
    registry.register_command("music", cmd_music, "搜索音乐 <歌手 - 歌名>")
    registry.register_command("artist", cmd_artist, "搜索歌手信息 <歌手名>")
    application.add_handler(CallbackQueryHandler(on_music_select, pattern=r"^music_sel:"))
    application.add_handler(CommandHandler("music", cmd_music))
    application.add_handler(CommandHandler("artist", cmd_artist))
    log.info("music module loaded")
