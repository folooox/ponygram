"""
RSS subscription module.

Commands (group admins or bot admins):
  /rss add <url> [label]   — subscribe current chat to a feed
  /rss del <id>            — unsubscribe
  /rss list                — list active subscriptions
  /rss pause <id>          — pause a subscription
  /rss resume <id>         — resume a paused subscription
  /rss interval <minutes>  — set polling interval (default 15, min 5)

Scheduler
  An APScheduler job fetches all active feeds every N minutes (per-bot
  setting stored in bot_data). New entries are pushed to their respective
  chats. Deduplication is done via SHA-256 of entry guid/link.

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
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler

from bot.database import (
    add_rss_feed,
    get_all_active_feeds,
    get_rss_feeds,
    is_entry_sent,
    mark_entry_sent,
    mark_feed_fetched,
    prune_sent_history,
    remove_rss_feed,
    set_feed_paused,
)
from bot.logger import get_logger
from bot.permissions import admin_only, group_only
from bot.router import registry

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
# Command handlers
# ---------------------------------------------------------------------------

@admin_only
async def cmd_rss(update: Update, context) -> None:
    """Router for /rss subcommands."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    args = context.args or []
    sub = args[0].lower() if args else ""

    if sub == "add":
        await _rss_add(update, context, args[1:])
    elif sub == "del":
        await _rss_del(update, context, args[1:])
    elif sub == "list":
        await _rss_list(update, context)
    elif sub == "pause":
        await _rss_pause(update, context, args[1:], pause=True)
    elif sub == "resume":
        await _rss_pause(update, context, args[1:], pause=False)
    elif sub == "interval":
        await _rss_interval(update, context, args[1:])
    else:
        await msg.reply_text(
            "Usage:\n"
            "  /rss add <url> [label]\n"
            "  /rss del <id>\n"
            "  /rss list\n"
            "  /rss pause <id>\n"
            "  /rss resume <id>\n"
            "  /rss interval <minutes>"
        )


async def _rss_add(update: Update, context, args: list) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    if not args:
        await msg.reply_text("Usage: /rss add <url> [label]")
        return

    url = args[0]
    label = " ".join(args[1:]) if len(args) > 1 else ""

    # Validate by fetching
    wait_msg = await msg.reply_text("⏳ Checking feed…")
    parsed = await _fetch_feed(url)
    if not parsed:
        await wait_msg.edit_text("❌ Could not fetch or parse that URL. Please check it and try again.")
        return

    feed_title = parsed.feed.get("title", url)
    user = update.effective_user
    feed = await add_rss_feed(
        chat_id=chat.id,
        url=url,
        label=label,
        added_by=user.id if user else 0,
    )

    display = label or feed_title
    await wait_msg.edit_text(
        f"✅ Subscribed to <b>{display}</b> (ID: <code>{feed.id}</code>)\n"
        f"Found {len(parsed.entries)} entries. New entries will be pushed every poll cycle.",
        parse_mode=ParseMode.HTML,
    )
    log.info("RSS feed added", feed_id=feed.id, chat_id=chat.id, url=url)


async def _rss_del(update: Update, context, args: list) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    if not args or not args[0].isdigit():
        await msg.reply_text("Usage: /rss del <id>  (get the ID from /rss list)")
        return

    feed_id = int(args[0])
    removed = await remove_rss_feed(feed_id, chat.id)
    if removed:
        await msg.reply_text(f"✅ Unsubscribed from feed <code>{feed_id}</code>.", parse_mode=ParseMode.HTML)
        log.info("RSS feed removed", feed_id=feed_id, chat_id=chat.id)
    else:
        await msg.reply_text("❌ Feed not found or it doesn't belong to this chat.")


async def _rss_list(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    feeds = await get_rss_feeds(chat.id)
    if not feeds:
        await msg.reply_text("No RSS subscriptions yet. Use /rss add <url> to subscribe.")
        return

    lines = ["<b>RSS Subscriptions</b>\n"]
    for f in feeds:
        status = "⏸ paused" if f.paused else "▶️ active"
        name = f.label or f.url
        fetched = f.last_fetched.strftime("%m-%d %H:%M") if f.last_fetched else "never"
        lines.append(
            f"[<code>{f.id}</code>] {status}\n"
            f"  {name}\n"
            f"  Last fetched: {fetched}"
        )

    await msg.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


async def _rss_pause(update: Update, context, args: list, pause: bool) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    action = "pause" if pause else "resume"
    if not args or not args[0].isdigit():
        await msg.reply_text(f"Usage: /rss {action} <id>")
        return

    feed_id = int(args[0])
    ok = await set_feed_paused(feed_id, chat.id, pause)
    if ok:
        word = "paused" if pause else "resumed"
        await msg.reply_text(f"✅ Feed <code>{feed_id}</code> {word}.", parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text("❌ Feed not found or it doesn't belong to this chat.")


async def _rss_interval(update: Update, context, args: list) -> None:
    msg = update.effective_message
    assert msg

    if not args or not args[0].isdigit():
        current = context.bot_data.get(_BOT_DATA_INTERVAL_KEY, _DEFAULT_INTERVAL)
        await msg.reply_text(f"Current poll interval: {current} minutes.\nUsage: /rss interval <minutes> (min 5)")
        return

    minutes = max(5, int(args[0]))
    context.bot_data[_BOT_DATA_INTERVAL_KEY] = minutes

    # Reschedule the job
    jobs = context.job_queue.get_jobs_by_name("rss_poll")
    for job in jobs:
        job.schedule_removal()
    context.job_queue.run_repeating(
        lambda ctx: _poll_feeds(ctx.application),
        interval=minutes * 60,
        first=10,
        name="rss_poll",
    )
    await msg.reply_text(f"✅ RSS poll interval set to {minutes} minutes.")
    log.info("RSS interval changed", minutes=minutes)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    registry.register_command("rss", cmd_rss, "Manage RSS subscriptions", admin_only=True)
    application.add_handler(CommandHandler("rss", cmd_rss))

    interval = application.bot_data.get(_BOT_DATA_INTERVAL_KEY, _DEFAULT_INTERVAL)
    application.job_queue.run_repeating(
        lambda ctx: _poll_feeds(ctx.application),
        interval=interval * 60,
        first=30,          # first run 30s after startup
        name="rss_poll",
    )
    log.info("rss module loaded", poll_interval_min=interval)
