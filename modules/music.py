"""
Music search module — powered by Last.fm API.

Commands:
  /music <artist - track>   — search for a track
  /artist <name>            — get artist info + top tracks

Requires LASTFM_API_KEY in .env.
Results cached 30 minutes per query.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import music_cache
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

_LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def _get_api_key(context) -> Optional[str]:
    cfg = context.bot_data.get("config")
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


async def cmd_music(update: Update, context) -> None:
    """Search for a track. Format: /music <artist - track> or /music <track name>"""
    msg = update.effective_message
    assert msg

    if not context.args:
        await msg.reply_text(
            "Usage: /music <track name>\n"
            "Or:    /music <artist> - <track>"
        )
        return

    api_key = _get_api_key(context)
    if not api_key:
        await msg.reply_text(
            "⚠️ Last.fm API key not configured. Set <code>LASTFM_API_KEY</code> in your .env file.",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = " ".join(context.args)
    cache_key = f"track:{raw.lower()}"

    cached = await music_cache.get(cache_key)
    if cached:
        results = cached
    else:
        # Try to split artist - track
        artist_q, track_q = "", raw
        if " - " in raw:
            parts = raw.split(" - ", 1)
            artist_q, track_q = parts[0].strip(), parts[1].strip()

        params = {"track": track_q, "limit": 5}
        if artist_q:
            params["artist"] = artist_q
        data = await _lastfm("track.search", api_key, **params)
        if not data:
            await msg.reply_text("⚠️ Could not reach Last.fm. Please try again later.")
            return
        results = data.get("results", {}).get("trackmatches", {}).get("track", [])
        if not results:
            await msg.reply_text(f"No tracks found for <b>{raw}</b>.", parse_mode=ParseMode.HTML)
            return
        await music_cache.set(cache_key, results)

    top = results[0]
    # Fetch detailed info for the top result
    detail_key = f"track_info:{top.get('artist','')}:{top.get('name','')}".lower()
    detail = await music_cache.get(detail_key)
    if not detail:
        d = await _lastfm("track.getInfo", api_key,
                          artist=top.get("artist", ""), track=top.get("name", ""))
        detail = d.get("track", top) if d else top
        await music_cache.set(detail_key, detail)

    text = _format_track(detail)

    buttons = []
    for i, t in enumerate(results[1:4], start=2):
        a = t.get("artist", "?")
        n = t.get("name", "?")[:25]
        buttons.append(InlineKeyboardButton(
            f"{i}. {n} — {a[:15]}",
            callback_data=f"music:track:{cache_key}:{i-1}",
        ))
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                         disable_web_page_preview=True)


async def cmd_artist(update: Update, context) -> None:
    msg = update.effective_message
    assert msg

    if not context.args:
        await msg.reply_text("Usage: /artist <artist name>")
        return

    api_key = _get_api_key(context)
    if not api_key:
        await msg.reply_text(
            "⚠️ Last.fm API key not configured. Set <code>LASTFM_API_KEY</code> in your .env file.",
            parse_mode=ParseMode.HTML,
        )
        return

    name = " ".join(context.args)
    cache_key = f"artist:{name.lower()}"

    cached = await music_cache.get(cache_key)
    if cached:
        artist, top_tracks = cached
    else:
        artist_data = await _lastfm("artist.getInfo", api_key, artist=name)
        top_data = await _lastfm("artist.getTopTracks", api_key, artist=name, limit=5)
        if not artist_data:
            await msg.reply_text("⚠️ Could not reach Last.fm. Please try again later.")
            return
        artist = artist_data.get("artist", {})
        if not artist:
            await msg.reply_text(f"Artist <b>{name}</b> not found.", parse_mode=ParseMode.HTML)
            return
        top_tracks = (top_data or {}).get("toptracks", {}).get("track", []) if top_data else []
        await music_cache.set(cache_key, (artist, top_tracks))

    text = _format_artist(artist, top_tracks)
    await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_music_button(update: Update, context) -> None:
    query = update.callback_query
    assert query
    await query.answer()

    _, kind, cache_key, idx_str = query.data.split(":", 3)
    idx = int(idx_str)
    api_key = _get_api_key(context)

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


def setup(application: Application) -> None:
    registry.register_command("music", cmd_music, "Search music tracks (Last.fm)")
    registry.register_command("artist", cmd_artist, "Get artist info (Last.fm)")
    application.add_handler(CommandHandler("music", cmd_music))
    application.add_handler(CommandHandler("artist", cmd_artist))
    application.add_handler(CallbackQueryHandler(on_music_button, pattern=r"^music:"))
    log.info("music module loaded")
