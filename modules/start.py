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
    "👋 *Welcome to Ponygram Bot!*\n\n"
    "I'm a modular Telegram bot with features for group management, "
    "RSS feeds, media parsing, queries, and more.\n\n"
    "Use /help to see all available commands."
)


async def start(update: Update, context) -> None:
    assert update.effective_message
    await update.effective_message.reply_text(_WELCOME, parse_mode=ParseMode.MARKDOWN)
    log.info("start command", user_id=update.effective_user and update.effective_user.id)


async def help_cmd(update: Update, context) -> None:
    assert update.effective_message
    user = update.effective_user
    is_admin = False
    if user and hasattr(context, "bot_data"):
        cfg = context.bot_data.get("config")
        if cfg:
            is_admin = cfg.is_admin(user.id)

    commands = registry.get_commands_list(is_admin=is_admin)
    text = f"*Available commands:*\n\n{commands}"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


def setup(application: Application) -> None:
    registry.register_command("start", start, "Start the bot")
    registry.register_command("help", help_cmd, "Show this help message")
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    log.info("start module loaded")
