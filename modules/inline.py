"""
Inline query module — routes @bot <prefix> <query> to media search backends.

Prefixes:
  movie  <title>          — TMDB movie search
  tv     <title>          — TMDB TV show search
  book   <query>          — Google Books search
  music  <artist - track> — Last.fm track search
  artist <name>           — Last.fm artist info
  game   <title>          — RAWG.io PS4/PS5 game search
"""

from __future__ import annotations

import uuid
from typing import List

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import Application, InlineQueryHandler

from bot.logger import get_logger

log = get_logger(__name__)

_HELP_TEXT = (
    "Search with inline mode:\n"
    "  <b>movie</b> &lt;title&gt;\n"
    "  <b>tv</b> &lt;title&gt;\n"
    "  <b>book</b> &lt;query&gt;\n"
    "  <b>music</b> &lt;artist - track&gt;\n"
    "  <b>artist</b> &lt;name&gt;\n"
    "  <b>game</b> &lt;title&gt;  — PS4/PS5 game search"
)


def _result(title: str, text: str, description: str = "",
            thumb: str = "") -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title=title,
        description=description,
        thumbnail_url=thumb or None,
        input_message_content=InputTextMessageContent(
            message_text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        ),
    )


async def on_inline_query(update: Update, context) -> None:
    query = update.inline_query
    if not query:
        return

    raw = (query.query or "").strip()

    if not raw:
        await query.answer(
            [_result("How to use inline mode", _HELP_TEXT, "Type a prefix + query")],
            cache_time=60,
        )
        return

    parts = raw.split(None, 1)
    prefix = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    results: List[InlineQueryResultArticle] = []

    try:
        if prefix in ("movie", "tv") and rest:
            from modules.movie import search_tmdb, _format_movie, _IMG_BASE
            items = await search_tmdb(rest, prefix, context)
            if items is None:
                results.append(_result("⚠️ API key not configured",
                                       "TMDB API key not set. Configure it in Web Admin → Settings."))
            elif not items:
                results.append(_result(f"No results for '{rest}'",
                                       f"No {prefix} results found."))
            else:
                for item in items[:5]:
                    title = item.get("title") or item.get("name", "Unknown")
                    year = (item.get("release_date") or item.get("first_air_date", ""))[:4]
                    desc = f"{year}" if year else ""
                    text = _format_movie(item, prefix)
                    poster = item.get("poster_path")
                    thumb = f"{_IMG_BASE}{poster}" if poster else ""
                    results.append(_result(title, text, desc, thumb))

        elif prefix == "book" and rest:
            from modules.book import search_books, _format_book, get_cover_url
            items = await search_books(rest)
            if not items:
                results.append(_result(f"No books for '{rest}'", "No books found."))
            else:
                for item in items[:5]:
                    info = item.get("volumeInfo", {})
                    title = info.get("title", "Unknown")
                    authors = ", ".join(info.get("authors", []))
                    year = info.get("publishedDate", "")[:4]
                    desc = f"{authors} ({year})" if year else authors
                    text = _format_book(item)
                    thumb = get_cover_url(item) or ""
                    results.append(_result(title, text, desc, thumb))

        elif prefix == "music" and rest:
            from modules.music import (search_tracks, _format_track, _lastfm,
                                       music_cache, _get_api_key, get_track_image,
                                       _fetch_track_detail)
            tracks = await search_tracks(rest, context)
            if tracks is None:
                results.append(_result("⚠️ API key not configured",
                                       "Last.fm API key not set. Configure it in Web Admin → Settings."))
            elif not tracks:
                results.append(_result(f"No tracks for '{rest}'", "No tracks found."))
            else:
                api_key = await _get_api_key(context)
                for t in tracks[:5]:
                    name = t.get("name", "?")
                    artist = t.get("artist", "")
                    artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist)
                    detail = {}
                    if api_key:
                        detail = await _fetch_track_detail(api_key, artist_name, name)
                    text = _format_track(detail or t)
                    thumb = get_track_image(detail) if detail else ""
                    results.append(_result(f"{name} — {artist_name}", text, artist_name, thumb or ""))

        elif prefix == "artist" and rest:
            from modules.music import search_artist, _format_artist, get_artist_image
            found = await search_artist(rest, context)
            if found is None:
                results.append(_result("⚠️ Not found or API key missing",
                                       f"Artist '{rest}' not found or Last.fm key not configured."))
            else:
                artist, top_tracks = found
                name = artist.get("name", rest)
                text = _format_artist(artist, top_tracks)
                thumb = get_artist_image(artist) or ""
                results.append(_result(name, text, "Last.fm artist", thumb))

        elif prefix == "game" and rest:
            from modules.psn import search_games, _format_game
            items = await search_games(rest)
            if items is None:
                results.append(_result("⚠️ API key not configured",
                                       "RAWG.io API key not set. Configure it in Web Admin → Settings."))
            elif not items:
                results.append(_result(f"No games for '{rest}'", "No PS4/PS5 games found."))
            else:
                for item in items[:5]:
                    name = item.get("name", "Unknown")
                    released = (item.get("released") or "")[:4]
                    rating = item.get("rating", 0)
                    desc = f"{released}  ⭐{rating:.1f}" if released else (f"⭐{rating:.1f}" if rating else "")
                    text = _format_game(item)
                    thumb = item.get("background_image") or ""
                    results.append(_result(name, text, desc, thumb))

        else:
            results.append(_result("How to use inline mode", _HELP_TEXT, "Type a prefix + query"))

    except Exception as e:
        log.warning("Inline query error", prefix=prefix, error=str(e))
        results.append(_result("Error", f"An error occurred: {e}", ""))

    await query.answer(results, cache_time=30)


def setup(application: Application) -> None:
    application.add_handler(InlineQueryHandler(on_inline_query))
    log.info("inline module loaded")
