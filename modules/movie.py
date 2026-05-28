"""
Movie & TV query module — powered by TMDB API.

Accessible via Telegram inline mode: @bot movie <title>  /  @bot tv <title>
Requires TMDB_API_KEY (env or Web Admin UI → Settings).
Results cached for 1 hour per query string.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

import aiohttp

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import movie_cache
from bot.database import get_bot_config
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

_TMDB_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_TMDB_URL = "https://www.themoviedb.org"


async def _get_api_key(context) -> Optional[str]:
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
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
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


async def search_tmdb(query: str, media_type: str, context) -> Optional[list]:
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


def _movie_select_keyboard(items: list, cache_key: str, media_type: str) -> InlineKeyboardMarkup:
    rows: list = []
    row: list = []
    for i, item in enumerate(items[:5]):
        title = (item.get("title") or item.get("name", "?"))[:18]
        year = (item.get("release_date") or item.get("first_air_date", ""))[:4]
        label = f"{i+1}. {title}（{year}）" if year else f"{i+1}. {title}"
        row.append(InlineKeyboardButton(label, callback_data=f"movie_sel:{media_type}:{cache_key}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def on_movie_select(update: Update, context) -> None:
    q = update.callback_query
    assert q
    await q.answer()

    _, media_type, cache_key, idx_str = q.data.split(":", 3)
    idx = int(idx_str)

    results = await movie_cache.get(cache_key)
    if not results or idx >= len(results):
        await q.edit_message_text("结果已过期，请重新搜索。")
        return

    await q.edit_message_text("⏳ 加载中…", reply_markup=None)

    item = results[idx]
    text = _format_movie(item, media_type)
    poster = item.get("poster_path")
    chat_id = q.message.chat_id if q.message else None

    try:
        await q.message.delete()
    except Exception:
        pass

    if poster and chat_id:
        try:
            await context.bot.send_photo(
                chat_id, f"{_IMG_BASE}{poster}",
                caption=text[:1024], parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass
    if chat_id:
        await context.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=False,
        )


async def _cmd_search(update: Update, context, media_type: str) -> None:
    msg = update.effective_message
    if not context.args:
        label = "电影" if media_type == "movie" else "剧集"
        cmd = "movie" if media_type == "movie" else "tv"
        await msg.reply_text(f"用法：/{cmd} <{label}名>")
        return
    query = " ".join(context.args)
    status = await msg.reply_text("🔍 搜索中…")
    results = await search_tmdb(query, media_type, context)
    await status.delete()

    if results is None:
        await msg.reply_text("❌ TMDB API 未配置，请在 Web 面板 → 设置 中添加 TMDB API Key")
        return
    if not results:
        await msg.reply_text(f"❌ 未找到：{query}")
        return

    short_key = hashlib.md5(f"{media_type}:{query.lower()}".encode()).hexdigest()[:16]
    await movie_cache.set(short_key, results)

    if len(results) == 1:
        item = results[0]
        text = _format_movie(item, media_type)
        poster = item.get("poster_path")
        if poster:
            try:
                await msg.reply_photo(f"{_IMG_BASE}{poster}", caption=text[:1024],
                                      parse_mode=ParseMode.HTML)
                return
            except Exception:
                pass
        await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        emoji = "🎬" if media_type == "movie" else "📺"
        lines = [f"{emoji} 搜到 <b>{len(results)}</b> 个结果，请选择：\n"]
        for i, item in enumerate(results[:5]):
            title = item.get("title") or item.get("name", "?")
            year = (item.get("release_date") or item.get("first_air_date", ""))[:4]
            rating = item.get("vote_average", 0)
            line = f"{i+1}. <b>{title}</b>"
            if year:    line += f" ({year})"
            if rating:  line += f" · ⭐{rating:.1f}"
            lines.append(line)
        keyboard = _movie_select_keyboard(results, short_key, media_type)
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                             reply_markup=keyboard)


async def cmd_movie(update: Update, context) -> None:
    await _cmd_search(update, context, "movie")


async def cmd_tv(update: Update, context) -> None:
    await _cmd_search(update, context, "tv")


def setup(application: Application) -> None:
    registry.register_command("movie", cmd_movie, "搜索电影 <片名>")
    registry.register_command("tv", cmd_tv, "搜索剧集 <剧名>")
    application.add_handler(CallbackQueryHandler(on_movie_select, pattern=r"^movie_sel:"))
    application.add_handler(CommandHandler("movie", cmd_movie))
    application.add_handler(CommandHandler("tv", cmd_tv))
    log.info("movie module loaded")
