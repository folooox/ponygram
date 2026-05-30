"""
Web article parser + subscription push module.

Commands:
  /parse <url>              — fetch any URL, extract article, post to Telegraph
  /subscribe <url> [label]  — subscribe to a webpage for periodic article push
  /unsubscribe <id>         — remove a web feed subscription
  /webfeeds                 — list subscriptions in the current chat

The /parse command is available to all users; /subscribe and friends are
admin-only in groups (private chats have no restriction).

Subscription polling runs every 30 minutes (configurable via bot_config
key "web_feed_interval"). For each subscribed URL, the bot fetches the
page, extracts outgoing article links, and pushes previously unseen ones
to the subscribed chat as Telegraph links.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from bot.ai import deepseek_call
from bot.database import (
    add_web_feed,
    get_all_active_web_feeds,
    get_web_feeds,
    is_url_sent,
    mark_url_sent,
    mark_web_feed_fetched,
    prune_web_sent,
    remove_web_feed,
    get_bot_config,
)
from bot.logger import get_logger
from bot.permissions import admin_only
from bot.router import registry
from bot.telegraph import (
    _ensure_telegraph_token,
    _parse_inline_md,
    _post_to_telegraph,
)

log = get_logger(__name__)

_ARTICLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# URL path patterns that indicate navigation/utility pages rather than articles
_NAV_PATTERNS = re.compile(
    r"/(login|logout|register|signup|about|contact|tag|tags|category|categories"
    r"|page/\d|search|feed|rss|sitemap|privacy|terms|author|user|account|help)",
    re.IGNORECASE,
)


def _get_proxy() -> Optional[str]:
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or None
    )


# ---------------------------------------------------------------------------
# Article extraction
# ---------------------------------------------------------------------------

async def _fetch_article(url: str) -> Optional[dict]:
    """
    Fetch a URL and extract article content using trafilatura.
    Returns {"title", "author", "image", "text", "source_url"} or None.
    """
    try:
        import trafilatura
        from trafilatura.metadata import extract_metadata
    except ImportError:
        log.error("trafilatura not installed — run: pip install trafilatura")
        return None

    proxy = _get_proxy()
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            headers={"User-Agent": _ARTICLE_UA},
            follow_redirects=True,
            timeout=httpx.Timeout(20, read=60),
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            page_html = r.text
    except Exception as e:
        log.warning("Article fetch failed", url=url, error=str(e))
        return None

    try:
        meta = extract_metadata(page_html, default_url=url)
        text = trafilatura.extract(
            page_html,
            url=url,
            include_images=False,
            include_tables=False,
            favor_recall=True,
            deduplicate=True,
        )
    except Exception as e:
        log.warning("trafilatura extraction failed", url=url, error=str(e))
        return None

    if not text:
        return None

    return {
        "title":      (getattr(meta, "title",  "") or "").strip(),
        "author":     (getattr(meta, "author", "") or "").strip(),
        "image":      (getattr(meta, "image",  "") or "").strip(),
        "text":       text,
        "source_url": url,
    }


def _article_to_nodes(article: dict) -> list:
    """Convert an article dict to Telegraph page content nodes."""
    nodes = []

    if article.get("image"):
        nodes.append({"tag": "figure", "children": [
            {"tag": "img", "attrs": {"src": article["image"]}}
        ]})

    if article.get("author"):
        nodes.append({"tag": "p", "children": [
            {"tag": "i", "children": [article["author"]]}
        ]})

    for para in article["text"].split("\n\n"):
        para = para.strip()
        if para:
            nodes.append({"tag": "p", "children": _parse_inline_md(para)})

    return nodes or [{"tag": "p", "children": ["暂无内容"]}]


# ---------------------------------------------------------------------------
# /parse command
# ---------------------------------------------------------------------------

async def cmd_parse(update: Update, context) -> None:
    """Parse any URL and return a Telegraph reading-mode link."""
    args = context.args
    if not args:
        await update.effective_message.reply_text("用法：/parse <url>")
        return

    url = args[0].strip().rstrip(".,;:!?)'\"")
    if not url.startswith(("http://", "https://")):
        await update.effective_message.reply_text("❌ 请提供有效的 URL（以 http:// 或 https:// 开头）")
        return

    msg = update.effective_message
    status = await msg.reply_text("⏳ 解析中…")

    try:
        article = await _fetch_article(url)
        if not article:
            await status.edit_text("❌ 无法提取正文，该页面可能需要登录或不含文章内容。")
            return

        token = await _ensure_telegraph_token()
        if not token:
            await status.edit_text("❌ Telegraph 服务不可用，请检查网络连接。")
            return

        nodes  = _article_to_nodes(article)
        tg_url = await _post_to_telegraph(article["title"] or "Article", nodes, token)

        # Optionally get a one-line AI summary (best-effort, no failure on error)
        summary_line = ""
        if article["text"]:
            summary = await deepseek_call(
                "article", "summarize",
                [{"role": "user",
                  "content": f"用一句话总结以下文章（不超过80字）：\n\n{article['text'][:1500]}"}],
                max_tokens=100,
            )
            if summary:
                summary_line = f"\n<i>{html.escape(summary[:200])}</i>"

        title_line = f"<b>{html.escape(article['title'][:200])}</b>\n" if article["title"] else ""
        await status.delete()
        await msg.reply_text(
            f"{title_line}{summary_line}\n📖 <a href=\"{tg_url}\">阅读全文</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("cmd_parse error", url=url, error=str(e))
        await status.edit_text(f"❌ 解析失败：{html.escape(str(e)[:200])}")


# ---------------------------------------------------------------------------
# Subscription commands (admin-only in groups)
# ---------------------------------------------------------------------------

@admin_only
async def cmd_subscribe(update: Update, context) -> None:
    """Subscribe to a webpage for periodic article push."""
    args = context.args
    if not args:
        await update.effective_message.reply_text("用法：/subscribe <url> [标签]")
        return

    url = args[0].strip()
    label = " ".join(args[1:]).strip() if len(args) > 1 else ""
    if not url.startswith(("http://", "https://")):
        await update.effective_message.reply_text("❌ 请提供有效的 URL")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    feed = await add_web_feed(chat_id, url, label=label, added_by=user_id)

    display = label or urlparse(url).netloc or url
    await update.effective_message.reply_text(
        f"✅ 已订阅 <b>{html.escape(display)}</b>（ID: {feed.id}）\n"
        f"将每 30 分钟检查一次新文章。",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_unsubscribe(update: Update, context) -> None:
    """Remove a web feed subscription by ID."""
    args = context.args
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("用法：/unsubscribe <id>（用 /webfeeds 查看 ID）")
        return

    feed_id = int(args[0])
    chat_id = update.effective_chat.id
    removed = await remove_web_feed(feed_id, chat_id)
    if removed:
        await update.effective_message.reply_text(f"✅ 已取消订阅 ID {feed_id}")
    else:
        await update.effective_message.reply_text(f"❌ 未找到属于本会话的订阅 ID {feed_id}")


@admin_only
async def cmd_webfeeds(update: Update, context) -> None:
    """List active web feed subscriptions for this chat."""
    chat_id = update.effective_chat.id
    feeds = await get_web_feeds(chat_id)
    if not feeds:
        await update.effective_message.reply_text("当前没有网页订阅。使用 /subscribe <url> 添加。")
        return

    lines = ["<b>网页订阅列表：</b>"]
    for f in feeds:
        status = "⏸ 暂停" if f.paused else "▶️ 运行中"
        name = html.escape(f.label or urlparse(f.url).netloc or f.url[:50])
        lines.append(f"{f.id}. {status} <b>{name}</b>\n   <code>{html.escape(f.url[:80])}</code>")

    await update.effective_message.reply_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Link extraction helpers (for subscription polling)
# ---------------------------------------------------------------------------

class _LinkExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self._base = base_url
        self._domain = urlparse(base_url).netloc
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_d = dict(attrs)
        href = (attrs_d.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return
        if href.startswith("//"):
            href = "https:" + href
        elif not href.startswith("http"):
            href = urljoin(self._base, href)
        parsed = urlparse(href)
        # same domain only
        if parsed.netloc != self._domain:
            return
        # strip query and fragment for dedup
        clean = parsed._replace(query="", fragment="").geturl()
        # at least 2 path segments (to exclude root/category pages)
        path = parsed.path.rstrip("/")
        if path.count("/") < 1:
            return
        # exclude navigation pages
        if _NAV_PATTERNS.search(path):
            return
        self.links.append(clean)


def _extract_links(html_content: str, base_url: str) -> list[str]:
    parser = _LinkExtractor(base_url)
    try:
        parser.feed(html_content)
    except Exception:
        pass
    # deduplicate while preserving order
    seen: set[str] = set()
    result = []
    for link in parser.links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


# ---------------------------------------------------------------------------
# Periodic poll job
# ---------------------------------------------------------------------------

async def _process_new_article(
    app,
    feed,
    article_url: str,
    url_hash: str,
) -> bool:
    """Fetch an article, post to Telegraph, send to chat. Returns True on success."""
    article = await _fetch_article(article_url)
    if not article:
        return False

    token = await _ensure_telegraph_token()
    if not token:
        return False

    try:
        nodes  = _article_to_nodes(article)
        tg_url = await _post_to_telegraph(article["title"] or "Article", nodes, token)
    except Exception as e:
        log.warning("Telegraph post failed in poll", feed_id=feed.id, error=str(e))
        return False

    # Translate title if it looks non-Chinese and translation is available
    display_title = article["title"]
    if display_title and not re.search(r"[一-鿿]", display_title):
        translated = await deepseek_call(
            "article", "translate",
            [{"role": "user",
              "content": f"Translate this article title to Simplified Chinese (title only, no explanation): {display_title}"}],
            max_tokens=60,
        )
        if translated:
            display_title = translated.strip('"').strip()

    label = html.escape(feed.label or urlparse(feed.url).netloc or "订阅")
    title_line = f"<b>{html.escape(display_title[:200])}</b>\n" if display_title else ""
    text = (
        f"📰 <b>{label}</b>\n"
        f"{title_line}"
        f"\n📖 <a href=\"{tg_url}\">阅读全文</a>"
    )
    try:
        await app.bot.send_message(
            feed.chat_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("Send to chat failed", feed_id=feed.id, chat_id=feed.chat_id, error=str(e))
        return False

    await mark_url_sent(feed.id, url_hash)
    return True


async def _poll_web_feeds(app) -> None:
    feeds = await get_all_active_web_feeds()
    if not feeds:
        return

    proxy = _get_proxy()
    for feed in feeds:
        try:
            await _poll_single_feed(app, feed, proxy)
        except Exception as e:
            log.warning("Web feed poll error", feed_id=feed.id, error=str(e))


async def _poll_single_feed(app, feed, proxy: Optional[str]) -> None:
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            headers={"User-Agent": _ARTICLE_UA},
            follow_redirects=True,
            timeout=httpx.Timeout(15, read=30),
        ) as client:
            r = await client.get(feed.url)
            r.raise_for_status()
            page_html = r.text
    except Exception as e:
        log.warning("Feed fetch failed", feed_id=feed.id, url=feed.url, error=str(e))
        await mark_web_feed_fetched(feed.id)
        return

    links = _extract_links(page_html, feed.url)
    sent_count = 0

    for link in links[:30]:  # cap at 30 candidate links per cycle
        url_hash = hashlib.sha256(link.encode()).hexdigest()
        if await is_url_sent(feed.id, url_hash):
            continue

        success = await _process_new_article(app, feed, link, url_hash)
        if success:
            sent_count += 1
            if sent_count >= 3:  # max 3 new articles per poll cycle
                break
        await asyncio.sleep(2)  # be polite between requests

    await prune_web_sent(feed.id, keep=300)
    await mark_web_feed_fetched(feed.id)

    if sent_count:
        log.info("Web feed pushed articles", feed_id=feed.id, count=sent_count)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    async def _get_interval() -> int:
        val = await get_bot_config("web_feed_interval")
        try:
            return max(5, int(val)) if val else 30
        except ValueError:
            return 30

    async def _start_poll_job(app):
        interval = await _get_interval()
        app.job_queue.run_repeating(
            lambda ctx: _poll_web_feeds(ctx.application),
            interval=interval * 60,
            first=90,
            name="web_feed_poll",
        )
        log.info("article module loaded", poll_interval_min=interval)

    application.job_queue.run_once(lambda ctx: _start_poll_job(ctx.application), when=5)

    registry.register_command("parse",       cmd_parse,       "解析网页文章到 Telegraph")
    registry.register_command("subscribe",   cmd_subscribe,   "订阅网页定时推送新文章",   admin_only=True)
    registry.register_command("unsubscribe", cmd_unsubscribe, "取消网页订阅",             admin_only=True)
    registry.register_command("webfeeds",    cmd_webfeeds,    "查看当前网页订阅列表",     admin_only=True)

    application.add_handler(CommandHandler("parse",       cmd_parse))
    application.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CommandHandler("webfeeds",    cmd_webfeeds))
