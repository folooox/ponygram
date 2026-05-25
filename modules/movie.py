"""
Movie & TV query module — powered by TMDB API.

Accessible via Telegram inline mode: @bot movie <title>  /  @bot tv <title>
Requires TMDB_API_KEY (env or Web Admin UI → Settings).
Results cached for 1 hour per query string.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp
from typing import Optional as _Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler

from bot.cache import movie_cache
from bot.database import get_bot_config
from bot.logger import get_logger

log = get_logger(__name__)

_TMDB_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_TMDB_URL = "https://www.themoviedb.org"


async def _get_api_key(context) -> _Optional[str]:
    key = await get_bot_config("tmdb_api_key")
    if key:
        return key
    cfg = context.bot_data.get("config") if context else None
    return getattr(cfg, "tmdb_api_key", None) if cfg else None


async def _tmdb_get(path: str, api_key: str, **params) -> Optional[Dict]:
    url = f"{_TMDB_BASE}{path}"
    params["api_key"] = api_key
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        log.warning("TMDB request failed", error=str(e))
        return None


def _stars(rating: float) -> str:
    filled = round(rating / 2)
    return "⭐" * filled + "☆" * (5 - filled)


def _format_movie(item: Dict, media_type: str = "movie") -> str:
    title = item.get("title") or item.get("name", "Unknown")
    original = item.get("original_title") or item.get("original_name", "")
    year_raw = item.get("release_date") or item.get("first_air_date", "")
    year = year_raw[:4] if year_raw else "N/A"
    rating = item.get("vote_average", 0)
    votes = item.get("vote_count", 0)
    overview = item.get("overview", "No description available.")
    if len(overview) > 300:
        overview = overview[:300].rstrip() + "…"
    tmdb_id = item.get("id")
    url = f"{_TMDB_URL}/{media_type}/{tmdb_id}"

    header = f"🎬 <b>{title}</b>" if media_type == "movie" else f"📺 <b>{title}</b>"
    lines = [header]
    if original and original != title:
        lines.append(f"<i>{original}</i>")
    lines.append(f"📅 {year}  |  {_stars(rating)} {rating:.1f}/10 ({votes:,} votes)")
    lines.append(f"\n{overview}")
    lines.append(f'\n🔗 <a href="{url}">TMDB page</a>')
    return "\n".join(lines)


async def search_tmdb(query: str, media_type: str, context) -> _Optional[list]:
    """Search TMDB and return a list of up to 5 result dicts, or None on error."""
    api_key = await _get_api_key(context)
    if not api_key:
        return None
    cache_key = f"{media_type}:{query.lower()}"
    cached = await movie_cache.get(cache_key)
    if cached:
        return cached
    endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
    data = await _tmdb_get(endpoint, api_key, query=query, language="zh-CN,en-US")
    if not data or not data.get("results"):
        return []
    results = data["results"][:5]
    await movie_cache.set(cache_key, results)
    return results


async def on_tmdb_button(update: Update, context) -> None:
    """Show a different result when user taps a numbered button."""
    query = update.callback_query
    assert query
    await query.answer()

    _, media_type, cache_key, idx_str = query.data.split(":", 3)
    idx = int(idx_str)

    results = await movie_cache.get(cache_key)
    if not results or idx >= len(results):
        await query.edit_message_caption("Result no longer cached. Please search again.")
        return

    item = results[idx]
    text = _format_movie(item, media_type)
    # Keep the same buttons but let user switch
    buttons = []
    for i, r in enumerate(results[1:4], start=2):
        title = (r.get("title") or r.get("name", "?"))[:30]
        year = (r.get("release_date") or r.get("first_air_date", ""))[:4]
        label = f"{i}. {title} ({year})" if year else f"{i}. {title}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"tmdb:{media_type}:{cache_key}:{i-1}"))
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

    poster = item.get("poster_path")
    if poster and query.message and query.message.photo:
        await query.edit_message_caption(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def setup(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(on_tmdb_button, pattern=r"^tmdb:"))
    log.info("movie module loaded")
