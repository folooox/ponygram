"""
AI dialogue module — powered by Claude (Anthropic).

Commands:
  /chat <message>     — send a message to Claude, continues conversation history
  /ask <message>      — alias for /chat
  /clearchat          — reset conversation history for this user in this chat
  /aichat on|off      — toggle AI auto-reply when the bot is @mentioned (admin only)

Conversation history is kept in memory per (user_id, chat_id) pair.
Each history is capped at MAX_HISTORY_TURNS turns to stay within token limits.
Streaming is used so long responses don't time out.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.database import get_group_settings, set_group_field
from bot.logger import get_logger
from bot.permissions import admin_only
from bot.router import registry

log = get_logger(__name__)

MAX_HISTORY_TURNS = 20       # per user per chat (each turn = user + assistant)
MAX_OUTPUT_TOKENS = 4096

# key: (user_id, chat_id)  →  list of {"role": ..., "content": ...}
_history: dict[tuple[int, int], list[dict]] = defaultdict(list)

_SYSTEM_PROMPT = (
    "You are Ponygram, a helpful and friendly Telegram bot assistant. "
    "Answer concisely and accurately. "
    "Format responses using plain text; avoid Markdown since Telegram's HTML "
    "mode is used — use <b>bold</b>, <i>italic</i>, and <code>code</code> "
    "HTML tags if needed. "
    "Keep replies under 3000 characters when possible."
)


def _get_client():
    """Lazily import and return an Anthropic AsyncAnthropic client."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package is not installed. "
            "Run: pip install anthropic"
        )
    import os
    api_key = os.environ.get("CLAUDE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "CLAUDE_API_KEY is not set in the environment. "
            "Add it to your .env file."
        )
    return anthropic.AsyncAnthropic(api_key=api_key)


def _trim_history(key: tuple[int, int]) -> None:
    """Keep only the most recent MAX_HISTORY_TURNS turns."""
    h = _history[key]
    # Each turn is 2 messages (user + assistant); keep 2 * MAX_HISTORY_TURNS
    max_msgs = MAX_HISTORY_TURNS * 2
    if len(h) > max_msgs:
        _history[key] = h[-max_msgs:]


async def _stream_claude(messages: list[dict], status_msg) -> str:
    """
    Stream Claude's response, progressively editing status_msg every ~1.5 s.
    Intermediate edits use plain text to avoid broken HTML mid-stream.
    Returns the complete response text.
    """
    client = _get_client()
    full_text = ""
    last_edit_time = 0.0
    EDIT_INTERVAL = 1.5  # seconds between live edits

    async with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=MAX_OUTPUT_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=messages,
        thinking={"type": "adaptive"},
    ) as stream:
        async for chunk in stream.text_stream:
            full_text += chunk
            now = asyncio.get_event_loop().time()
            if now - last_edit_time >= EDIT_INTERVAL and full_text.strip():
                try:
                    await status_msg.edit_text(full_text.rstrip() + " ✍️")
                    last_edit_time = now
                except TelegramError:
                    pass

    return full_text.strip()


async def _handle_ai_message(update: Update, context, user_text: str) -> None:
    """Core: send user_text to Claude with live streaming, update history."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    assert msg and user and chat

    key = (user.id, chat.id)
    _history[key].append({"role": "user", "content": user_text})
    _trim_history(key)

    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    status = await msg.reply_text("💭 Thinking…")

    try:
        response_text = await _stream_claude(list(_history[key]), status)
    except RuntimeError as e:
        await status.edit_text(f"❌ {e}")
        _history[key].pop()
        return
    except Exception as e:
        log.warning("Claude API error", error=str(e))
        await status.edit_text("❌ Failed to get a response from Claude. Please try again.")
        _history[key].pop()
        return

    if not response_text:
        await status.edit_text("❌ Received an empty response. Please try again.")
        _history[key].pop()
        return

    _history[key].append({"role": "assistant", "content": response_text})

    final = response_text if len(response_text) <= 4000 else response_text[:3997] + "…"

    try:
        await status.edit_text(final, parse_mode=ParseMode.HTML)
    except TelegramError:
        try:
            await status.edit_text(final)
        except TelegramError as e:
            log.warning("Failed to send Claude response", error=str(e))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_chat(update: Update, context) -> None:
    """/chat <message> — send a message to Claude AI."""
    msg = update.effective_message
    assert msg

    if not context.args:
        await msg.reply_text(
            "Usage: /chat <your message>\n\n"
            "Example: /chat What's the capital of France?\n\n"
            "Use /clearchat to reset the conversation history."
        )
        return

    user_text = " ".join(context.args)
    await _handle_ai_message(update, context, user_text)


async def cmd_ask(update: Update, context) -> None:
    """/ask <message> — alias for /chat."""
    await cmd_chat(update, context)


async def cmd_clearchat(update: Update, context) -> None:
    """/clearchat — reset conversation history for this user in this chat."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    assert msg and user and chat

    key = (user.id, chat.id)
    turn_count = len(_history[key]) // 2
    _history[key].clear()

    await msg.reply_text(
        f"🗑️ Conversation history cleared ({turn_count} turn{'s' if turn_count != 1 else ''} removed).\n"
        "Starting fresh — say /chat <message> to begin a new conversation."
    )


@admin_only
async def cmd_aichat(update: Update, context) -> None:
    """/aichat on|off — toggle AI auto-reply on @mention (admin only)."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    if not context.args or context.args[0].lower() not in ("on", "off"):
        settings = await get_group_settings(chat.id)
        state = "on" if getattr(settings, "aichat_enabled", False) else "off"
        await msg.reply_text(
            f"AI mention-reply is currently <b>{state}</b>.\n"
            f"Use /aichat on or /aichat off to change.",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = context.args[0].lower() == "on"
    await set_group_field(chat.id, aichat_enabled=enabled)
    await msg.reply_text(
        f"✅ AI mention-reply {'enabled' if enabled else 'disabled'}."
    )


# ---------------------------------------------------------------------------
# Auto-reply when bot is @mentioned
# ---------------------------------------------------------------------------

async def on_mention(update: Update, context) -> None:
    """Reply with AI when the bot is @mentioned in a group chat."""
    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not chat or not msg.text:
        return

    settings = await get_group_settings(chat.id)
    if not getattr(settings, "aichat_enabled", False):
        return

    bot_username = (context.bot.username or "").lower()
    if not bot_username:
        return

    text = msg.text
    user_text = text
    found = False

    for entity in (msg.entities or []):
        if entity.type == "mention":
            mentioned = text[entity.offset + 1 : entity.offset + entity.length].lower()
            if mentioned == bot_username:
                user_text = (text[: entity.offset] + text[entity.offset + entity.length :]).strip()
                found = True
                break

    if not found:
        return

    if not user_text:
        await msg.reply_text(
            "Yes? Ask me something — mention me with your question, "
            "e.g. @BotName What is Python?"
        )
        return

    await _handle_ai_message(update, context, user_text)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    registry.register_command("chat", cmd_chat, "Chat with Claude AI")
    registry.register_command("ask", cmd_ask, "Ask Claude AI a question")
    registry.register_command("clearchat", cmd_clearchat, "Reset AI conversation history")
    registry.register_command("aichat", cmd_aichat, "Toggle AI mention-reply (admin)", admin_only=True)

    application.add_handler(CommandHandler("chat", cmd_chat))
    application.add_handler(CommandHandler("ask", cmd_ask))
    application.add_handler(CommandHandler("clearchat", cmd_clearchat))
    application.add_handler(CommandHandler("aichat", cmd_aichat))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Entity("mention"),
            on_mention,
        ),
        group=30,
    )

    log.info("ai_chat module loaded")
