"""
Book search module — powered by Google Books API.

Accessible via Telegram inline mode: @bot book <query>
No API key required for basic usage (up to 1000 req/day per IP).
Results cached 1 hour per query.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import book_cache
from bot.logger import get_logger

log = get_logger(__name__)

_GBOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"


async def _gbooks_search(query: str, max_results: int = 5) -> Optional[List[Dict]]:
    params = {"q": query, "maxResults": max_results, "printType": "books", "langRestrict": ""}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_GBOOKS_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data.get("items") or []
    except Exception as e:
        log.warning("Google Books request failed", error=str(e))
        return None


def _format_book(item: Dict) -> str:
    info = item.get("volumeInfo", {})
    title = info.get("title", "Unknown Title")
    subtitle = info.get("subtitle", "")
    authors = ", ".join(info.get("authors", ["Unknown Author"]))
    publisher = info.get("publisher", "")
    published = info.get("publishedDate", "")[:4]
    pages = info.get("pageCount")
    rating = info.get("averageRating")
    ratings_count = info.get("ratingsCount", 0)
    description = info.get("description", "No description available.")
    if len(description) > 300:
        description = description[:300].rstrip() + "…"
    categories = ", ".join(info.get("categories", []))
    link = info.get("infoLink", "")

    lines = [f"📚 <b>{title}</b>"]
    if subtitle:
        lines.append(f"<i>{subtitle}</i>")
    lines.append(f"✍️ {authors}")

    meta = []
    if publisher:
        meta.append(publisher)
    if published:
        meta.append(published)
    if pages:
        meta.append(f"{pages} pages")
    if meta:
        lines.append("📖 " + " · ".join(meta))

    if rating:
        stars = "⭐" * round(rating) + "☆" * (5 - round(rating))
        lines.append(f"{stars} {rating:.1f}/5 ({ratings_count:,} ratings)")

    if categories:
        lines.append(f"🏷 {categories}")

    lines.append(f"\n{description}")

    if link:
        lines.append(f'\n🔗 <a href="{link}">Google Books</a>')

    return "\n".join(lines)


async def search_books(query: str) -> list:
    """Search books and return up to 5 results; empty list on no results."""
    cache_key = f"book:{query.lower()}"
    cached = await book_cache.get(cache_key)
    if cached:
        return cached
    items = await _gbooks_search(query)
    if items:
        await book_cache.set(cache_key, items)
    return items or []


async def on_book_button(update: Update, context) -> None:
    query = update.callback_query
    assert query
    await query.answer()

    _, cache_key, idx_str = query.data.split(":", 2)
    idx = int(idx_str)

    items = await book_cache.get(cache_key)
    if not items or idx >= len(items):
        await query.edit_message_text("Result no longer cached. Please search again.")
        return

    item = items[idx]
    text = _format_book(item)

    buttons = []
    for i, r in enumerate(items[1:4], start=2):
        info = r.get("volumeInfo", {})
        title = info.get("title", "?")[:30]
        year = info.get("publishedDate", "")[:4]
        label = f"{i}. {title} ({year})" if year else f"{i}. {title}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"book:{cache_key}:{i-1}"))
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

    if query.message and query.message.photo:
        await query.edit_message_caption(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cmd_book(update: Update, context) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("用法：/book <书名或关键词>")
        return
    query = " ".join(context.args)
    status = await msg.reply_text("🔍 搜索中…")
    items = await search_books(query)
    await status.delete()
    if not items:
        await msg.reply_text(f"❌ 未找到：{query}")
        return

    short_key = hashlib.md5(f"book:{query.lower()}".encode()).hexdigest()[:16]
    await book_cache.set(short_key, items)

    text = _format_book(items[0])
    buttons = []
    for i, r in enumerate(items[1:4], start=2):
        info = r.get("volumeInfo", {})
        title = info.get("title", "?")[:28]
        year = info.get("publishedDate", "")[:4]
        label = f"{i}. {title}（{year}）" if year else f"{i}. {title}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"book:{short_key}:{i-1}"))
    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                         disable_web_page_preview=True)


def setup(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(on_book_button, pattern=r"^book:"))
    application.add_handler(CommandHandler("book", cmd_book))
    log.info("book module loaded")
