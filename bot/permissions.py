"""
Permission decorators and helpers for handler functions.

Provides
--------
admin_only      — Telegram chat admin OR bot admin (config.is_admin)
owner_only      — bot owner only
group_only      — rejects commands in private chats
not_blacklisted — rejects updates from blacklisted users
"""

from __future__ import annotations

import functools
from typing import Callable, Coroutine, Any

from telegram import Update, ChatMember
from telegram.ext import CallbackContext

from bot.logger import get_logger

log = get_logger(__name__)


def _get_config(context: CallbackContext):
    return context.bot_data.get("config")


def admin_only(func: Callable) -> Callable:
    """Allow only Telegram chat admins or bot admins. Works in groups and DMs."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: CallbackContext) -> Any:
        user = update.effective_user
        if not user:
            return
        cfg = _get_config(context)
        # Bot-level admin always passes
        if cfg and cfg.is_admin(user.id):
            return await func(update, context)
        # In a group, check Telegram admin status
        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup"):
            member = await chat.get_member(user.id)
            if member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                return await func(update, context)
        await update.effective_message.reply_text(
            "❌ 仅群管理员可使用此命令"
        )
    return wrapper


def owner_only(func: Callable) -> Callable:
    """Allow only the bot owner (OWNER_ID in config)."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: CallbackContext) -> Any:
        user = update.effective_user
        cfg = _get_config(context)
        if user and cfg and cfg.is_owner(user.id):
            return await func(update, context)
        await update.effective_message.reply_text(
            "❌ 仅 Bot 所有者可使用此命令"
        )
    return wrapper


def group_only(func: Callable) -> Callable:
    """Reject the command when used outside a group/supergroup."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: CallbackContext) -> Any:
        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup"):
            return await func(update, context)
        await update.effective_message.reply_text(
            "❌ 此命令仅可在群组中使用"
        )
    return wrapper


def not_blacklisted(func: Callable) -> Callable:
    """Silently ignore messages from blacklisted users."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: CallbackContext) -> Any:
        from bot.database import is_blacklisted
        user = update.effective_user
        if user and await is_blacklisted(user.id):
            log.debug("Ignoring blacklisted user", user_id=user.id)
            return
        return await func(update, context)
    return wrapper
