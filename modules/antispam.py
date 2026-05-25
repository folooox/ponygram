"""
Anti-spam and anti-ad module.

Features
--------
- Rate limiting: mute users who send too many messages in a short window
- Anti-ad: delete messages containing Telegram invite links or URLs from
  non-admin users (configurable per group)

Admin commands
--------------
/antispam on|off            — toggle rate limiting
/antispam threshold <n> <s> — n messages per s seconds before mute
/antiad on|off              — toggle anti-advertising filter
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.database import get_group_settings, set_group_field
from bot.logger import get_logger
from bot.permissions import admin_only, group_only
from bot.router import registry

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


# ---------------------------------------------------------------------------
# Admin configuration commands
# ---------------------------------------------------------------------------

@group_only
@admin_only
async def cmd_antispam(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    args = context.args or []

    if not args:
        await msg.reply_text("Usage: /antispam on|off  OR  /antispam threshold <msgs> <secs>")
        return

    if args[0].lower() in ("on", "off"):
        enabled = args[0].lower() == "on"
        await set_group_field(chat.id, antispam_enabled=enabled)
        await msg.reply_text(f"✅ Anti-spam {'enabled' if enabled else 'disabled'}.")
        return

    if args[0].lower() == "threshold" and len(args) == 3:
        try:
            n, s = int(args[1]), int(args[2])
        except ValueError:
            await msg.reply_text("Both values must be integers.")
            return
        n = max(1, min(n, 50))
        s = max(1, min(s, 300))
        await set_group_field(chat.id, antispam_max_msgs=n, antispam_window=s)
        await msg.reply_text(f"✅ Threshold: {n} messages per {s} seconds.")
        return

    await msg.reply_text("Usage: /antispam on|off  OR  /antispam threshold <msgs> <secs>")


@group_only
@admin_only
async def cmd_antiad(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await msg.reply_text("Usage: /antiad on|off")
        return
    enabled = context.args[0].lower() == "on"
    await set_group_field(chat.id, antiad_enabled=enabled)
    await msg.reply_text(f"✅ Anti-advertising {'enabled' if enabled else 'disabled'}.")


def setup(application: Application) -> None:
    # Listen to all group text messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            on_message,
        ),
        group=10,  # run at lower priority than command handlers
    )

    cmds = [
        ("antispam", cmd_antispam, "Configure anti-spam (on/off/threshold)", True),
        ("antiad",   cmd_antiad,   "Toggle anti-advertising filter",          True),
    ]
    for name, handler, desc, admin in cmds:
        registry.register_command(name, handler, desc, admin_only=admin)
        application.add_handler(CommandHandler(name, handler))

    log.info("antispam module loaded")
