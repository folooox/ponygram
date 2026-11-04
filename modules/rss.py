"""Admin-owned RSS feeds; destinations must be Telegram channels or groups."""
import asyncio
import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
import aiohttp
import feedparser
from telegram.constants import ParseMode
from telegram.error import TelegramError
from bot.ai import polish_text
from bot.database import (get_all_active_feeds, is_entry_sent, mark_entry_sent,
                          mark_feed_fetched, prune_sent_history)
from bot.logger import get_logger
log = get_logger(__name__)
ALLOWED_TYPES = {'group', 'supergroup', 'channel'}


def normalize_target(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.startswith(('https://t.me/', 'http://t.me/', 't.me/')):
        value = '@' + value.split('t.me/', 1)[1].strip('/').split('?')[0]
    if re.fullmatch(r'-[1-9][0-9]*', value) or re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}', value):
        return value
    raise ValueError('目标必须是频道或群组的负数 Chat ID、@用户名或公开 t.me 链接，不能是个人用户。')


async def resolve_target(bot, target):
    target = normalize_target(target)
    if not target:
        return None
    chat = await bot.get_chat(int(target) if target.startswith('-') else target)
    if chat.type not in ALLOWED_TYPES:
        raise ValueError('RSS 不允许推送到个人私聊。')
    return chat.id


async def _fetch_feed(url):
    if urlparse(url).scheme not in ('http', 'https'):
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                raw = await r.read()
        parsed = feedparser.parse(raw)
        return parsed if parsed.entries or not parsed.bozo else None
    except (aiohttp.ClientError, TimeoutError):
        return None


def _clean_summary(raw, max_len=300):
    text = html.unescape(re.sub(r'<[^>]*>', '', raw or ''))
    return re.sub(r'\s+', ' ', text).strip()[:max_len]


def _format_entry(source, title, summary, entry):
    esc = html.escape
    link = entry.get('link', '')
    link = link if urlparse(link).scheme in ('http', 'https') else ''
    lines = [f'📰 <b>{esc(source[:120])}</b>', f'<b>{esc(title[:200])}</b>']
    published = entry.get('published_parsed') or entry.get('updated_parsed')
    if published:
        lines.append('发布时间：' + datetime(*published[:6], tzinfo=timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    if summary:
        lines.append('\n' + esc(summary[:350]))
    if link:
        lines.append(f'\n🔗 <a href="{esc(link, quote=True)}">阅读原文</a>')
    return '\n'.join(lines)


async def _poll_feeds(app):
    for row in await get_all_active_feeds():
        # Legacy positive chat IDs are never treated as destinations.
        target = row.target or (str(row.chat_id) if row.chat_id and row.chat_id < 0 else '')
        if not target:
            continue
        try:
            destination = await resolve_target(app.bot, target)
            parsed = await _fetch_feed(row.url)
            if not parsed:
                continue
            if not row.last_fetched:
                # First fetch establishes a baseline rather than flooding old entries.
                for entry in parsed.entries:
                    await mark_entry_sent(row.id, entry)
                await mark_feed_fetched(row.id)
                continue
            fresh = [e for e in parsed.entries if not await is_entry_sent(row.id, e)]
            for entry in list(reversed(fresh))[:5]:
                title = entry.get('title') or '未命名文章'
                raw = entry.get('summary') or (entry.get('content') or [{}])[0].get('value', '')
                summary = _clean_summary(raw)
                if row.ai_enabled:
                    title, summary = await asyncio.gather(polish_text('rss', title), polish_text('rss', summary))
                await app.bot.send_message(destination, _format_entry(row.label or parsed.feed.get('title') or 'RSS', title, summary, entry),
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                await mark_entry_sent(row.id, entry)
                await asyncio.sleep(.5)
            await mark_feed_fetched(row.id)
            await prune_sent_history(row.id)
        except (TelegramError, ValueError) as e:
            log.warning('RSS target or delivery failed', feed_id=row.id, error_type=type(e).__name__)


def setup(application):
    interval = max(5, application.bot_data.get('rss_interval', 15))
    application.job_queue.run_repeating(lambda ctx: _poll_feeds(ctx.application), interval=interval * 60,
                                         first=30, name='rss_poll')
