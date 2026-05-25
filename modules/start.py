"""
Built-in /start and /help command handlers.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.constants import ParseMode

from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

_WELCOME = (
    "👋 <b>Welcome to Ponygram Bot!</b>\n\n"
    "I'm a modular Telegram bot with features for group management, "
    "media parsing, AI dialogue, and more.\n\n"
    "• Paste a YouTube / TikTok / Instagram URL → auto-download\n"
    "• @mention me, say <b>pony …</b>, or reply to me → AI chat\n"
    "• Use <b>@{username} movie avengers</b> for inline media search\n\n"
    "Use /help to see available commands."
)


async def start(update: Update, context) -> None:
    assert update.effective_message
    bot_username = context.bot.username or "bot"
    await update.effective_message.reply_text(
        _WELCOME.replace("{username}", bot_username),
        parse_mode=ParseMode.HTML,
    )
    log.info("start command", user_id=update.effective_user and update.effective_user.id)


async def help_cmd(update: Update, context) -> None:
    assert update.effective_message
    text = (
        "<b>Commands</b>\n\n"
        "<b>Info</b>\n"
        "/start — welcome message\n"
        "/help — this message\n"
        "/id — your Telegram ID\n"
        "/whois — detailed user info\n"
        "/warns — check warnings\n\n"
        "<b>Group moderation</b> (admins only)\n"
        "/mute /unmute — restrict messaging\n"
        "/kick /ban /unban — remove users\n"
        "/warn /clearwarns — warn system\n"
        "/pin /unpin — pin messages\n\n"
        "<b>Natural triggers</b>\n"
        "• @mention me → AI reply\n"
        "• Start with <b>pony </b> → AI reply\n"
        "• Reply to my messages → AI reply\n"
        "• Paste a media URL → auto-download\n\n"
        "<b>Inline mode</b>\n"
        "@{username} movie / tv / book / music / artist &lt;query&gt;"
    )
    bot_username = context.bot.username or "bot"
    await update.effective_message.reply_text(
        text.replace("{username}", bot_username),
        parse_mode=ParseMode.HTML,
    )


def setup(application: Application) -> None:
    registry.register_command("start", start, "Start the bot")
    registry.register_command("help", help_cmd, "Show available commands")
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    log.info("start module loaded")
