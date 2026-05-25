"""
RSS subscription module.

Feeds are managed via the Web Admin UI (/rss).
An APScheduler job fetches all active feeds every N minutes and pushes
new entries to their respective chats.

Push format
  📰 <Feed label or title>
  <entry title> (linked)
  <summary — up to 300 chars, HTML stripped>
  🔗 <link>
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import List, Optional

import aiohttp
import feedparser
import html2text
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application

from bot.database import (
    get_all_active_feeds,
    is_entry_sent,
    mark_entry_sent,
    mark_feed_fetched,
    prune_sent_history,
)
from bot.logger import get_logger

log = get_logger(__name__)

_DEFAULT_INTERVAL = 15   # minutes
_BOT_DATA_INTERVAL_KEY = "rss_interval"
_h2t = html2text.HTML2Text()
_h2t.ignore_links = True
_h2t.ignore_images = True
_h2t.body_width = 0


# ---------------------------------------------------------------------------
# Feed fetching helpers
# ---------------------------------------------------------------------------

async def _fetch_feed(url: str) -> Optional[feedparser.FeedParserDict]:
    """Download and parse an RSS/Atom feed. Returns None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("RSS fetch non-200", url=url, status=resp.status)
                    return None
                raw = await resp.read()
        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            log.warning("RSS parse error", url=url, error=str(parsed.bozo_exception))
            return None
        return parsed
    except Exception as e:
        log.warning("RSS fetch error", url=url, error=str(e))
        return None


def _clean_summary(raw: str, max_len: int = 300) -> str:
    text = _h2t.handle(raw).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def _format_entry(feed_title: str, feed_label: Optional[str], entry) -> str:
    title = getattr(entry, "title", "Untitled")
    link = getattr(entry, "link", "")
    summary_raw = ""
    if hasattr(entry, "summary"):
        summary_raw = entry.summary
    elif hasattr(entry, "content") and entry.content:
        summary_raw = entry.content[0].get("value", "")

    summary = _clean_summary(summary_raw) if summary_raw else ""
    display_name = feed_label or feed_title or "RSS"

    lines = [f"📰 <b>{display_name}</b>"]
    if link:
        lines.append(f'<a href="{link}">{title}</a>')
    else:
        lines.append(f"<b>{title}</b>")
    if summary:
        lines.append(f"\n{summary}")
    if link:
        lines.append(f'\n🔗 <a href="{link}">Read more</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------

async def _poll_feeds(app: Application) -> None:
    """Fetch all active feeds and push new entries. Runs as a repeating job."""
    feeds = await get_all_active_feeds()
    if not feeds:
        return

    log.debug("RSS poll started", feed_count=len(feeds))

    for feed_row in feeds:
        parsed = await _fetch_feed(feed_row.url)
        if not parsed:
            continue

        feed_title = parsed.feed.get("title", "")
        new_entries: List = []

        for entry in parsed.entries:
            if not await is_entry_sent(feed_row.id, entry):
                new_entries.append(entry)

        # Push oldest first, cap at 5 per poll cycle to avoid flooding
        for entry in reversed(new_entries[:5]):
            text = _format_entry(feed_title, feed_row.label, entry)
            try:
                await app.bot.send_message(
                    feed_row.chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                await mark_entry_sent(feed_row.id, entry)
                await asyncio.sleep(0.5)  # gentle rate limiting
            except TelegramError as e:
                log.warning("RSS push failed", feed_id=feed_row.id, error=str(e))
                break

        await mark_feed_fetched(feed_row.id)
        await prune_sent_history(feed_row.id)

    log.debug("RSS poll finished")


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    interval = application.bot_data.get(_BOT_DATA_INTERVAL_KEY, _DEFAULT_INTERVAL)
    application.job_queue.run_repeating(
        lambda ctx: _poll_feeds(ctx.application),
        interval=interval * 60,
        first=30,
        name="rss_poll",
    )
    log.info("rss module loaded", poll_interval_min=interval)
