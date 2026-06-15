"""
AI dialogue module — powered by Claude (Anthropic).

Natural triggers (no slash commands needed):
  1. @mention the bot in a group
  2. Start a message with "pony " (case-insensitive)
  3. Reply directly to a bot message

Claude API key is read from the BotConfig database table (set via Web Admin
UI → Settings), falling back to the CLAUDE_API_KEY environment variable.

Only active groups (is_active=True) get AI responses; private chats always work.
Per-group aichat_enabled toggle controls whether AI replies in that group.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters

from bot import render
from bot.components import (
    Component,
    ConfigField,
    TemplateSpec,
    is_module_enabled,
    register_component,
)
from bot.database import get_bot_config, get_group_settings
from bot.logger import get_logger
from bot.rich_message import send_rich, send_rich_draft
from bot.router import registry

log = get_logger(__name__)

MAX_HISTORY_TURNS = 20
MAX_OUTPUT_TOKENS = 4096

# key: (user_id, chat_id) → list of {"role": ..., "content": ...}
_history: dict[tuple[int, int], list[dict]] = defaultdict(list)

_SYSTEM_PROMPT = (
    "You are Ponygram, a helpful and friendly Telegram bot assistant. "
    "Answer concisely and accurately. "
    "Keep replies under 3000 characters when possible."
)

# Output-format directives appended to the (web-editable) system prompt at call
# time. Which one is used depends on the delivery path, not the template:
#   • Rich path (private chats, Bot API 10.1 sendRichMessage): Markdown.
#   • Legacy path (groups, and rich fallback): Telegram HTML.
_FMT_RICH = (
    "\n\nFormat your response in Markdown. Headings, **bold**, *italic*, "
    "`inline code`, fenced code blocks, bullet/numbered lists, tables, and "
    "> blockquotes are all supported and render natively. Do not use HTML tags."
)
_FMT_HTML = (
    "\n\nFormat your response using Telegram HTML: <b>bold</b>, <i>italic</i>, "
    "<code>code</code>, <pre> blocks, and <a href=\"…\">links</a>. "
    "Do not use Markdown syntax."
)


async def _get_api_key() -> Optional[str]:
    """Return Claude API key from DB (preferred) or environment."""
    try:
        key = await get_bot_config("claude_api_key")
        if key and key.strip():
            return key.strip()
    except Exception:
        pass
    import os
    return os.environ.get("CLAUDE_API_KEY", "").strip() or None


def _trim_history(key: tuple[int, int]) -> None:
    h = _history[key]
    max_msgs = MAX_HISTORY_TURNS * 2
    if len(h) > max_msgs:
        _history[key] = h[-max_msgs:]


async def _stream_claude(
    messages: list[dict],
    *,
    bot,
    chat_id: int,
    status_msg,
    use_rich: bool,
) -> str:
    """Stream a Claude response with a live preview, returning the full text.

    Two preview paths share the same ~1.5s throttle:
      • ``use_rich`` (private chats): push partials via ``sendRichMessageDraft``
        (animated rich draft); no status message is involved.
      • legacy (groups / fallback): edit ``status_msg`` with the growing text."""
    api_key = await _get_api_key()
    if not api_key:
        raise RuntimeError(
            "Claude API key not configured. Set it in the Web Admin UI (Settings page)."
        )
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.AsyncAnthropic(api_key=api_key)
    full_text = ""
    last_edit = 0.0
    EDIT_INTERVAL = 1.5

    # System prompt is editable in the web 组件中心 (falls back to default); the
    # output-format directive is chosen by the delivery path, not the template.
    system_prompt = await render.render("ai_chat", "system_prompt")
    system_prompt += _FMT_RICH if use_rich else _FMT_HTML

    async with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=messages,
        thinking={"type": "adaptive"},
    ) as stream:
        async for chunk in stream.text_stream:
            full_text += chunk
            now = asyncio.get_event_loop().time()
            if now - last_edit >= EDIT_INTERVAL and full_text.strip():
                if use_rich:
                    # Drafts are best-effort progress; ignore failures here — the
                    # final send_rich decides rich-vs-legacy delivery.
                    await send_rich_draft(bot, chat_id, full_text.rstrip())
                    last_edit = now
                else:
                    try:
                        await status_msg.edit_text(full_text.rstrip() + " ✍️")
                        last_edit = now
                    except TelegramError:
                        pass

    return full_text.strip()


async def _deliver_error(bot, chat_id: int, status_msg, text: str) -> None:
    """Show an error on the status message (legacy path) or as a fresh message
    (rich path, which has no status message)."""
    try:
        if status_msg is not None:
            await status_msg.edit_text(text)
        else:
            await bot.send_message(chat_id, text)
    except TelegramError:
        pass


async def _deliver_legacy(msg, status_msg, final: str) -> None:
    """Send the final text via the legacy HTML path, falling back to plain text.

    Edits ``status_msg`` when present (groups), otherwise sends a fresh reply
    (rich path that fell back after streaming)."""
    async def _put(text: str, **kw):
        if status_msg is not None:
            await status_msg.edit_text(text, **kw)
        else:
            await msg.reply_text(text, **kw)

    try:
        await _put(final, parse_mode=ParseMode.HTML)
    except TelegramError:
        try:
            await _put(final)
        except TelegramError as e:
            log.warning("Failed to send Claude response", error=str(e))


async def _handle_ai_message(update: Update, context, user_text: str) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    assert msg and user and chat

    key = (user.id, chat.id)
    _history[key].append({"role": "user", "content": user_text})
    _trim_history(key)

    bot = context.bot
    # Rich draft streaming is private-chat only (Bot API 10.1). Groups keep the
    # edit-based "typing" preview + HTML final exactly as before.
    use_rich = chat.type == "private"

    await bot.send_chat_action(chat.id, ChatAction.TYPING)
    status = None if use_rich else await msg.reply_text("💭 Thinking…")

    try:
        response_text = await _stream_claude(
            list(_history[key]), bot=bot, chat_id=chat.id,
            status_msg=status, use_rich=use_rich,
        )
    except RuntimeError as e:
        await _deliver_error(bot, chat.id, status, f"❌ {e}")
        _history[key].pop()
        return
    except Exception as e:
        log.warning("Claude API error", error=str(e))
        await _deliver_error(bot, chat.id, status,
                             "❌ Failed to get a response from Claude. Please try again.")
        _history[key].pop()
        return

    if not response_text:
        await _deliver_error(bot, chat.id, status,
                             "❌ Received an empty response. Please try again.")
        _history[key].pop()
        return

    _history[key].append({"role": "assistant", "content": response_text})

    # Rich delivery first (private chats). On any failure fall back to the legacy
    # send WITHOUT re-calling Claude — the response is already in hand.
    if use_rich and await send_rich(bot, chat.id, response_text, reply_to=msg.message_id):
        return

    final = response_text if len(response_text) <= 4000 else response_text[:3997] + "…"
    await _deliver_legacy(msg, status, final)


# ---------------------------------------------------------------------------
# Natural trigger handler
# ---------------------------------------------------------------------------

async def on_natural_trigger(update: Update, context) -> None:
    """Respond to @mention, 'pony' prefix, or reply to bot — no slash commands."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.text:
        return

    # Group chat: check activation and aichat toggle
    if chat.type in ("group", "supergroup"):
        settings = await get_group_settings(chat.id)
        if not settings.is_active:
            return
        if not settings.aichat_enabled:
            return

    text = msg.text
    bot_username = (context.bot.username or "").lower()
    user_text: Optional[str] = None

    # Trigger 1: @mention
    if bot_username:
        for entity in (msg.entities or []):
            if entity.type == "mention":
                mentioned = text[entity.offset + 1: entity.offset + entity.length].lower()
                if mentioned == bot_username:
                    user_text = (text[:entity.offset] + text[entity.offset + entity.length:]).strip()
                    break

    # Trigger 2: starts with "pony"
    if user_text is None:
        stripped = text.strip()
        lower = stripped.lower()
        if lower.startswith("pony "):
            user_text = stripped[5:].strip()
        elif lower == "pony":
            user_text = ""

    # Trigger 3: reply to a bot message
    if user_text is None:
        if msg.reply_to_message and msg.reply_to_message.from_user:
            if msg.reply_to_message.from_user.id == context.bot.id:
                user_text = text

    if user_text is None:
        return

    # Module disabled in 组件中心 → ignore the trigger silently.
    if not await is_module_enabled("ai_chat"):
        return

    if not user_text:
        await msg.reply_text("Yes? Ask me something!")
        return

    await _handle_ai_message(update, context, user_text)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

_COMPONENT = Component(
    id="ai_chat",
    name="AI 对话",
    icon="bi-robot",
    description="@提及、pony 前缀或回复机器人触发的 Claude 流式对话。系统提示词可在下方编辑。",
    config_keys=[
        ConfigField(
            key="claude_api_key",
            label="Claude API Key",
            kind="secret",
            help='前往 console.anthropic.com → API Keys → Create Key（仅显示一次）。格式：<code>sk-ant-api03-</code> 开头。',
            placeholder="sk-ant-api03-…",
            test_service="claude",
            link="https://console.anthropic.com",
            link_label="console.anthropic.com",
        ),
    ],
    templates=[
        TemplateSpec(
            key="system_prompt",
            name="系统提示词 (System Prompt)",
            default=_SYSTEM_PROMPT,
            help="定义 AI 的人设与回复风格。无占位符，整段即为发给模型的 system 提示。输出格式（私聊用富文本 Markdown，群聊用 HTML）会自动追加，无需在此说明。",
        ),
    ],
    order=10,
)


def setup(application: Application) -> None:
    register_component(_COMPONENT)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_natural_trigger,
        ),
        group=30,
    )
    log.info("ai_chat module loaded")
