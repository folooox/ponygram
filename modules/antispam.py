"""
Anti-spam and anti-ad module.

Features
--------
- Rate limiting: mute users who send too many messages in a short window
- Anti-ad: delete messages containing Telegram invite links

Configuration is done via the Web Admin UI (/groups/<id>).
Only active groups (is_active=True) are monitored.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, filters

from bot.database import get_group_settings
from bot.logger import get_logger

log = get_logger(__name__)

# Anti-ad: block Telegram group invite links (t.me/joinchat/… or t.me/+…)
_INVITE_RE = re.compile(
    r"(?:https?://)?t\.me/(?:joinchat/|\+)[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# In-memory rate-limit tracker: {chat_id: {user_id: deque of timestamps}}
_msg_times: Dict[int, Dict[int, Deque[float]]] = defaultdict(lambda: defaultdict(deque))


async def _is_chat_admin(update: Update) -> bool:
    """Return True if the sender is a Telegram admin in the chat."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    try:
        member = await chat.get_member(user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def on_message(update: Update, context) -> None:
    """Message filter — checks rate limit and anti-ad rules."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if user.is_bot:
        return

    cfg = context.bot_data.get("config")
    # Bot admins always bypass
    if cfg and cfg.is_admin(user.id):
        return
    # Telegram admins bypass
    if await _is_chat_admin(update):
        return

    settings = await get_group_settings(chat.id)
    if not settings.is_active:
        return

    # ------------------------------------------------------------------ #
    # Anti-ad check                                                        #
    # ------------------------------------------------------------------ #
    if settings.antiad_enabled and msg.text:
        if _INVITE_RE.search(msg.text):
            try:
                await msg.delete()
                await context.bot.send_message(
                    chat.id,
                    f"⚠️ Advertising / external links are not allowed here.",
                )
            except BadRequest:
                pass
            log.info("Anti-ad: deleted message", user_id=user.id, chat_id=chat.id)
            return

    # ------------------------------------------------------------------ #
    # Rate limiting                                                        #
    # ------------------------------------------------------------------ #
    if not settings.antispam_enabled:
        return

    max_msgs = settings.antispam_max_msgs
    window = settings.antispam_window
    now = time.monotonic()
    history = _msg_times[chat.id][user.id]

    # Prune old timestamps
    while history and now - history[0] > window:
        history.popleft()

    history.append(now)

    if len(history) > max_msgs:
        try:
            await chat.restrict_member(
                user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(time.time()) + 60,  # mute 60 seconds
            )
            await context.bot.send_message(
                chat.id,
                f"🔇 [{user.first_name}](tg://user?id={user.id}) was muted for 1 minute (flood).",
                parse_mode=ParseMode.MARKDOWN,
            )
        except BadRequest:
            pass
        history.clear()
        log.info("Anti-spam: muted flood user", user_id=user.id, chat_id=chat.id)


def setup(application: Application) -> None:
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            on_message,
        ),
        group=10,
    )
    log.info("antispam module loaded")
