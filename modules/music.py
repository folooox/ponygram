"""
Music search module — powered by Last.fm API.

Accessible via Telegram inline mode: @bot music <artist - track>  /  @bot artist <name>
Requires LASTFM_API_KEY (env or Web Admin UI → Settings).
Results cached 30 minutes per query.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import music_cache
from bot.database import get_bot_config
from bot.logger import get_logger

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
            async with session.get(_LASTFM_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
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


def _format_track(track: Dict) -> str:
    name = track.get("name", "Unknown")
    artist = track.get("artist", {})
    artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist)
    listeners = track.get("listeners", "")
    playcount = track.get("playcount", "")
    url = track.get("url", "")

    wiki = track.get("wiki", {})
    summary = wiki.get("summary", "") if wiki else ""
    # Strip Last.fm "Read more" anchor tags
    import re
    summary = re.sub(r'<a href="[^"]*">.*?</a>', "", summary).strip()
    summary = re.sub(r"<[^>]+>", "", summary)
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

    bio = artist.get("bio", {})
    summary = bio.get("summary", "") if bio else ""
    import re
    summary = re.sub(r'<a href="[^"]*">.*?</a>', "", summary).strip()
    summary = re.sub(r"<[^>]+>", "", summary)
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
            if t_url:
                lines.append(f'{i}. <a href="{t_url}">{t_name}</a>')
            else:
                lines.append(f"{i}. {t_name}")
    if url:
        lines.append(f'\n🔗 <a href="{url}">Last.fm</a>')
    return "\n".join(lines)


async def search_tracks(query: str, context) -> Optional[list]:
    """Search tracks and return list, or None on API error."""
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
    """Return (artist_dict, top_tracks) or None on error/not found."""
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
    await music_cache.set(cache_key, (artist, top_tracks))
    return artist, top_tracks


async def on_music_button(update: Update, context) -> None:
    query = update.callback_query
    assert query
    await query.answer()

    _, kind, cache_key, idx_str = query.data.split(":", 3)
    idx = int(idx_str)
    api_key = await _get_api_key(context)

    if kind == "track":
        results = await music_cache.get(cache_key)
        if not results or idx >= len(results):
            await query.edit_message_text("Result no longer cached. Please search again.")
            return
        top = results[idx]
        detail_key = f"track_info:{top.get('artist','')}:{top.get('name','')}".lower()
        detail = await music_cache.get(detail_key)
        if not detail and api_key:
            d = await _lastfm("track.getInfo", api_key,
                              artist=top.get("artist", ""), track=top.get("name", ""))
            detail = d.get("track", top) if d else top
            await music_cache.set(detail_key, detail)
        text = _format_track(detail or top)
        buttons = []
        for i, t in enumerate(results[1:4], start=2):
            a = t.get("artist", "?")
            n = t.get("name", "?")[:25]
            buttons.append(InlineKeyboardButton(
                f"{i}. {n} — {a[:15]}",
                callback_data=f"music:track:{cache_key}:{i-1}",
            ))
        keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=keyboard, disable_web_page_preview=True)


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

    api_key = await _get_api_key(context)
    top = results[0]
    detail = None
    if api_key:
        d = await _lastfm("track.getInfo", api_key,
                          artist=top.get("artist", ""), track=top.get("name", ""))
        detail = d.get("track", top) if d else top
    text = _format_track(detail or top)

    buttons = []
    for i, t in enumerate(results[1:4], start=2):
        a = t.get("artist", "?")
        n = t.get("name", "?")[:22]
        buttons.append(InlineKeyboardButton(
            f"{i}. {n} — {a[:12]}",
            callback_data=f"music:track:{short_key}:{i-1}",
        ))
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    await msg.reply_text(text, parse_mode=ParseMode.HTML,
                         reply_markup=keyboard, disable_web_page_preview=True)


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
    await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def setup(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(on_music_button, pattern=r"^music:"))
    application.add_handler(CommandHandler("music", cmd_music))
    application.add_handler(CommandHandler("artist", cmd_artist))
    log.info("music module loaded")
