"""
Group administration commands.

Commands (all require admin):
/mute   [duration] — restrict a user from sending messages
/unmute            — lift a mute
/kick              — remove a user from the group
/ban               — ban a user permanently
/unban             — lift a ban
/gblacklist        — add user to global bot blacklist (owner only)
/gunblacklist      — remove from global blacklist (owner only)

All commands work by replying to a message or passing a user ID / @username.
Duration for /mute: e.g. 1h, 30m, 2d  (default: indefinite)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler

from bot.database import add_to_blacklist, remove_from_blacklist, upsert_user
from bot.logger import get_logger
from bot.permissions import admin_only, group_only, owner_only
from bot.router import registry

log = get_logger(__name__)

_DURATION_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)
_DURATION_MAP = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(text: str) -> Optional[int]:
    """Return duration in seconds or None if unparseable."""
    m = _DURATION_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)) * _DURATION_MAP[m.group(2).lower()]


async def _resolve_target(update: Update, context) -> Tuple[Optional[int], str]:
    """
    Return (user_id, display_name) from a reply or first arg.
    Returns (None, "") if nothing could be resolved.
    """
    msg = update.effective_message
    # Prefer replied-to message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name or str(u.id)
    # Fall back to first argument (integer ID)
    if context.args:
        raw = context.args[0]
        if raw.lstrip("-").isdigit():
            return int(raw), raw
    return None, ""


@group_only
@admin_only
async def cmd_mute(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    until: Optional[datetime] = None
    duration_str = ""
    # Duration can be first arg (if no reply) or second arg (if reply)
    args = context.args or []
    raw_dur = args[1] if (msg.reply_to_message and len(args) >= 1) else (args[0] if not msg.reply_to_message and len(args) >= 2 else (args[0] if len(args) >= 1 and msg.reply_to_message else None))
    # Simpler: last arg that matches duration pattern
    for a in reversed(args):
        secs = _parse_duration(a)
        if secs:
            until = datetime.utcnow() + timedelta(seconds=secs)
            duration_str = f" for {a}"
            break

    try:
        await chat.restrict_member(
            user_id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
            until_date=until,
        )
        await msg.reply_text(
            f"🔇 Muted {name} (`{user_id}`){duration_str}.",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("User muted", user_id=user_id, chat_id=chat.id, until=until)
    except BadRequest as e:
        await msg.reply_text(f"Failed to mute: {e}")


@group_only
@admin_only
async def cmd_unmute(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    try:
        await chat.restrict_member(user_id, permissions=chat.permissions or ChatPermissions(
            can_send_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_send_polls=True,
        ))
        await msg.reply_text(f"🔊 Unmuted {name} (`{user_id}`).", parse_mode=ParseMode.MARKDOWN)
        log.info("User unmuted", user_id=user_id, chat_id=chat.id)
    except BadRequest as e:
        await msg.reply_text(f"Failed to unmute: {e}")


@group_only
@admin_only
async def cmd_kick(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    try:
        await chat.ban_member(user_id)
        await chat.unban_member(user_id)  # unban so they can rejoin
        await msg.reply_text(f"👢 Kicked {name} (`{user_id}`).", parse_mode=ParseMode.MARKDOWN)
        log.info("User kicked", user_id=user_id, chat_id=chat.id)
    except BadRequest as e:
        await msg.reply_text(f"Failed to kick: {e}")


@group_only
@admin_only
async def cmd_ban(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    reason = " ".join(context.args[1:]) if (msg.reply_to_message and context.args) else ""

    try:
        await chat.ban_member(user_id)
        text = f"🚫 Banned {name} (`{user_id}`)."
        if reason:
            text += f"\nReason: {reason}"
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        log.info("User banned", user_id=user_id, chat_id=chat.id, reason=reason)
    except BadRequest as e:
        await msg.reply_text(f"Failed to ban: {e}")


@group_only
@admin_only
async def cmd_unban(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    try:
        await chat.unban_member(user_id)
        await msg.reply_text(f"✅ Unbanned {name} (`{user_id}`).", parse_mode=ParseMode.MARKDOWN)
        log.info("User unbanned", user_id=user_id, chat_id=chat.id)
    except BadRequest as e:
        await msg.reply_text(f"Failed to unban: {e}")


@owner_only
async def cmd_gblacklist(update: Update, context) -> None:
    """Add a user to the global bot blacklist."""
    msg = update.effective_message
    assert msg

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    reason = " ".join(context.args) if context.args and not msg.reply_to_message else (
        " ".join(context.args[1:]) if context.args else ""
    )
    actor = update.effective_user
    await add_to_blacklist(user_id, reason=reason, added_by=actor.id if actor else 0)
    await msg.reply_text(
        f"⛔ `{user_id}` added to global blacklist.", parse_mode=ParseMode.MARKDOWN
    )
    log.info("User added to blacklist", user_id=user_id, reason=reason)


@owner_only
async def cmd_gunblacklist(update: Update, context) -> None:
    """Remove a user from the global bot blacklist."""
    msg = update.effective_message
    assert msg

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID.")
        return

    removed = await remove_from_blacklist(user_id)
    if removed:
        await msg.reply_text(
            f"✅ `{user_id}` removed from global blacklist.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await msg.reply_text(f"`{user_id}` was not in the blacklist.", parse_mode=ParseMode.MARKDOWN)


def setup(application: Application) -> None:
    cmds = [
        ("mute",           cmd_mute,           "Mute a user [duration: 1h/30m/2d]",   True),
        ("unmute",         cmd_unmute,         "Unmute a user",                        True),
        ("kick",           cmd_kick,           "Kick a user from the group",           True),
        ("ban",            cmd_ban,            "Ban a user from the group",            True),
        ("unban",          cmd_unban,          "Unban a user",                         True),
        ("gblacklist",     cmd_gblacklist,     "Add user to global blacklist",         True),
        ("gunblacklist",   cmd_gunblacklist,   "Remove user from global blacklist",    True),
    ]
    for name, handler, desc, admin in cmds:
        registry.register_command(name, handler, desc, admin_only=admin)
        application.add_handler(CommandHandler(name, handler))
    log.info("admin module loaded")
