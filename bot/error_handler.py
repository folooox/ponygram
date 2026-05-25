"""
Global error handler for the python-telegram-bot Application.

Registered via ``application.add_error_handler(error_handler)``.

Responsibilities:
- Classify common Telegram errors and log them at the appropriate severity.
- Send a friendly, non-leaking reply to the user when an update caused the error.
- Log full tracebacks only for unexpected exceptions.
"""

from __future__ import annotations

import traceback
from typing import Optional

from telegram import Update
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    TelegramError,
    TimedOut,
)
from telegram.ext import CallbackContext

from bot.logger import get_logger

log = get_logger(__name__)

# Message shown to the end user when something goes wrong.
_USER_FACING_ERROR = (
    "Sorry, something went wrong while processing your request. "
    "Please try again in a moment."
)


async def error_handler(update: object, context: CallbackContext) -> None:
    """
    Central error handler invoked by the Application dispatcher.

    Parameters
    ----------
    update:
        The incoming Update that triggered the error (may be None for
        background jobs or network-level errors).
    context:
        The callback context carrying ``context.error``.
    """
    error = context.error

    if error is None:
        log.warning("error_handler called with no error in context")
        return

    # ------------------------------------------------------------------ #
    # Classify and log                                                     #
    # ------------------------------------------------------------------ #
    if isinstance(error, TimedOut):
        log.warning(
            "Telegram request timed out",
            error=str(error),
        )
        # Transient — no user message needed; the library handles retry logic.
        return

    if isinstance(error, NetworkError):
        log.warning(
            "Network error communicating with Telegram",
            error=str(error),
            hint="Check your internet connection or Telegram API status.",
        )
        return

    if isinstance(error, Forbidden):
        # Bot was blocked or kicked from a chat.
        chat_id: Optional[int] = None
        if isinstance(update, Update) and update.effective_chat:
            chat_id = update.effective_chat.id
        log.info(
            "Bot was blocked or kicked",
            chat_id=chat_id,
            error=str(error),
        )
        return

    if isinstance(error, BadRequest):
        log.warning(
            "Telegram BadRequest error",
            error=str(error),
            update_id=getattr(update, "update_id", None),
        )
        await _reply_to_user(update, context)
        return

    if isinstance(error, TelegramError):
        log.error(
            "Unhandled TelegramError",
            error=str(error),
            update_id=getattr(update, "update_id", None),
        )
        await _reply_to_user(update, context)
        return

    # Completely unexpected exception — log the full traceback.
    tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
    log.error(
        "Unexpected exception in update handler",
        update_id=getattr(update, "update_id", None),
        traceback="".join(tb_lines),
    )
    await _reply_to_user(update, context)


async def _reply_to_user(update: object, context: CallbackContext) -> None:
    """
    Send a safe, non-leaking error message to the user if possible.

    Only attempts to reply when *update* is a real Update with a message or
    callback query — silently skips system-level errors where no chat is
    involved.
    """
    if not isinstance(update, Update):
        return

    try:
        if update.callback_query:
            await update.callback_query.answer(
                text="An error occurred. Please try again.", show_alert=False
            )
        elif update.effective_message:
            await update.effective_message.reply_text(_USER_FACING_ERROR)
    except Exception as reply_err:  # noqa: BLE001
        # Don't let the reply attempt itself raise another error.
        log.warning("Failed to send error reply to user", error=str(reply_err))
