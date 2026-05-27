"""
Book search module — powered by Google Books API.

Accessible via:
  @bot book <query>  — inline picker (up to 5 results with thumbnails)
  /book <query>      — direct command (cover art + navigation buttons)

No API key required (up to 1000 req/day per IP).
Results cached 1 hour per query.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import book_cache
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

_GBOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"


async def _gbooks_search(query: str, max_results: int = 5) -> Optional[List[Dict]]:
    params = {"q": query, "maxResults": max_results, "printType": "books"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_GBOOKS_BASE, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data.get("items") or []
    except Exception as e:
        log.warning("Google Books request failed", error=str(e))
        return None


def get_cover_url(item: Dict) -> Optional[str]:
    """Return an HTTPS cover image URL, or None."""
    links = item.get("volumeInfo", {}).get("imageLinks", {})
    url = links.get("thumbnail") or links.get("smallThumbnail")
    if not url:
        return None
    # Google Books returns HTTP; Telegram/PTB needs HTTPS
    url = url.replace("http://", "https://")
    # zoom=1 is tiny; zoom=0 is front-cover size
    url = url.replace("zoom=1", "zoom=0")
    return url


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
    description = (info.get("description") or "No description available.")
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


def _book_keyboard(items: list, current_idx: int, cache_key: str) -> Optional[InlineKeyboardMarkup]:
    """Row of buttons for switching results, excluding the one currently shown."""
    buttons = []
    for i, r in enumerate(items[:5]):
        if i == current_idx:
            continue
        info = r.get("volumeInfo", {})
        title = info.get("title", "?")[:20]
        year = info.get("publishedDate", "")[:4]
        label = f"{i+1}. {title}（{year}）" if year else f"{i+1}. {title}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"book:{cache_key}:{i}"))
        if len(buttons) >= 3:
            break
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def search_books(query: str) -> list:
    """Search books; returns up to 5 result dicts or empty list."""
    cache_key = f"book:{query.lower()}"
    cached = await book_cache.get(cache_key)
    if cached:
        return cached
    items = await _gbooks_search(query)
    if items:
        await book_cache.set(cache_key, items)
    return items or []


async def on_book_button(update: Update, context) -> None:
    q = update.callback_query
    assert q
    await q.answer()

    _, cache_key, idx_str = q.data.split(":", 2)
    idx = int(idx_str)

    items = await book_cache.get(cache_key)
    if not items or idx >= len(items):
        await q.edit_message_text("结果已过期，请重新搜索。")
        return

    item = items[idx]
    text = _format_book(item)
    keyboard = _book_keyboard(items, idx, cache_key)

    if q.message and q.message.photo:
        try:
            await q.edit_message_caption(text[:1024], parse_mode=ParseMode.HTML,
                                         reply_markup=keyboard)
        except Exception:
            pass
    else:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  reply_markup=keyboard, disable_web_page_preview=True)


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
    keyboard = _book_keyboard(items, 0, short_key)
    cover = get_cover_url(items[0])

    if cover:
        try:
            await msg.reply_photo(cover, caption=text[:1024],
                                  parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return
        except Exception:
            pass
    await msg.reply_text(text, parse_mode=ParseMode.HTML,
                         reply_markup=keyboard, disable_web_page_preview=True)


def setup(application: Application) -> None:
    registry.register_command("book", cmd_book, "搜索书籍 <书名>")
    application.add_handler(CallbackQueryHandler(on_book_button, pattern=r"^book:"))
    application.add_handler(CommandHandler("book", cmd_book))
    log.info("book module loaded")
