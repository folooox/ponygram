"""
Group administration commands.

Commands (all require Telegram group admin status):
/mute   [duration] — restrict a user from sending messages
/unmute            — lift a mute
/kick              — remove a user from the group
/ban               — ban a user permanently
/unban             — lift a ban
/warn   [reason]   — warn a user (auto-bans at warn_limit, configurable via Web UI)
/warns             — show warnings for a user
/clearwarns        — remove all warnings for a user
/pin   [loud]      — pin the replied-to message
/unpin             — unpin a message

All commands work by replying to a message or passing a user ID / @username.
Duration for /mute: e.g. 1h, 30m, 2d  (default: indefinite)
Blacklist and warn-limit are managed via the Web Admin UI.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler

from bot.database import (
    upsert_user,
    add_warn,
    get_warns,
    clear_warns,
    get_group_settings,
)
from bot.logger import get_logger
from bot.permissions import admin_only, group_only
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
    Supports: reply-to-message, integer user ID, or @username.
    Returns (None, "") if nothing could be resolved.
    """
    msg = update.effective_message
    chat = update.effective_chat
    # Prefer replied-to message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name or str(u.id)
    # Fall back to first argument: integer ID or @username
    if context.args:
        raw = context.args[0]
        if raw.lstrip("-").isdigit():
            return int(raw), raw
        if raw.startswith("@") and chat:
            try:
                member = await context.bot.get_chat_member(chat.id, raw)
                u = member.user
                return u.id, u.first_name or raw
            except Exception:
                pass
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
    for a in reversed(context.args or []):
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


# ---------------------------------------------------------------------------
# Warn system
# ---------------------------------------------------------------------------

@group_only
@admin_only
async def cmd_warn(update: Update, context) -> None:
    """/warn [reason] — warn a user; auto-bans at warn_limit."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID / @username.")
        return

    args = context.args or []
    reason = " ".join(args[1:] if not msg.reply_to_message and args else args)

    actor = update.effective_user
    count = await add_warn(chat.id, user_id, reason=reason, warned_by=actor.id if actor else 0)
    settings = await get_group_settings(chat.id)
    limit = settings.warn_limit or 3

    if count >= limit:
        try:
            await chat.ban_member(user_id)
        except BadRequest:
            pass
        await msg.reply_text(
            f"🚫 {name} (`{user_id}`) reached {count}/{limit} warnings and was banned."
            + (f"\nLast reason: {reason}" if reason else ""),
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("User auto-banned after warn limit", user_id=user_id, chat_id=chat.id)
    else:
        await msg.reply_text(
            f"⚠️ {name} (`{user_id}`) warned ({count}/{limit})."
            + (f"\nReason: {reason}" if reason else ""),
            parse_mode=ParseMode.MARKDOWN,
        )
    log.info("User warned", user_id=user_id, chat_id=chat.id, count=count)


@group_only
async def cmd_warns(update: Update, context) -> None:
    """/warns — show warnings for a user (reply or own stats if no target)."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        user = update.effective_user
        if user:
            user_id, name = user.id, user.first_name or str(user.id)
        else:
            await msg.reply_text("Reply to a message or provide a user ID.")
            return

    warns = await get_warns(chat.id, user_id)
    settings = await get_group_settings(chat.id)
    limit = settings.warn_limit or 3

    if not warns:
        await msg.reply_text(f"✅ {name} has no warnings in this chat.")
        return

    lines = [f"⚠️ *{name}* — {len(warns)}/{limit} warning(s):"]
    for i, w in enumerate(warns, 1):
        date_str = w.warned_at.strftime("%Y-%m-%d")
        reason_str = w.reason or "(no reason)"
        lines.append(f"  {i}. {date_str}: {reason_str}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@group_only
@admin_only
async def cmd_clearwarns(update: Update, context) -> None:
    """/clearwarns — remove all warnings for a user."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text("Reply to a message or provide a user ID / @username.")
        return

    count = await clear_warns(chat.id, user_id)
    await msg.reply_text(
        f"✅ Cleared {count} warning(s) for {name} (`{user_id}`).",
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info("Warnings cleared", user_id=user_id, chat_id=chat.id, count=count)


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------

@group_only
@admin_only
async def cmd_pin(update: Update, context) -> None:
    """/pin [loud] — pin the replied-to message; add 'loud' to notify members."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    if not msg.reply_to_message:
        await msg.reply_text("Reply to a message to pin it.")
        return

    loud = bool(context.args) and context.args[0].lower() == "loud"
    try:
        await context.bot.pin_chat_message(
            chat.id,
            msg.reply_to_message.message_id,
            disable_notification=not loud,
        )
        await msg.reply_text("📌 Message pinned.")
        log.info("Message pinned", chat_id=chat.id, msg_id=msg.reply_to_message.message_id)
    except BadRequest as e:
        await msg.reply_text(f"Failed to pin: {e}")


@group_only
@admin_only
async def cmd_unpin(update: Update, context) -> None:
    """/unpin — unpin the replied-to message (or most recent pin)."""
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    try:
        if msg.reply_to_message:
            await context.bot.unpin_chat_message(chat.id, msg.reply_to_message.message_id)
        else:
            await context.bot.unpin_chat_message(chat.id)
        await msg.reply_text("📌 Message unpinned.")
        log.info("Message unpinned", chat_id=chat.id)
    except BadRequest as e:
        await msg.reply_text(f"Failed to unpin: {e}")


async def cmd_rules(update: Update, context) -> None:
    """/rules — show this group's rules (set via Web Admin)."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    settings = await get_group_settings(chat.id)
    if settings.rules_text and settings.rules_text.strip():
        text = f"📋 <b>群规则</b>\n\n{settings.rules_text}"
    else:
        text = "📋 本群暂未设置规则。"
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


def setup(application: Application) -> None:
    cmds = [
        ("mute",       cmd_mute,       "Mute a user [duration: 1h/30m/2d]",  True),
        ("unmute",     cmd_unmute,     "Unmute a user",                       True),
        ("kick",       cmd_kick,       "Kick a user from the group",          True),
        ("ban",        cmd_ban,        "Ban a user from the group",           True),
        ("unban",      cmd_unban,      "Unban a user",                        True),
        ("warn",       cmd_warn,       "Warn a user (auto-ban at limit)",     True),
        ("warns",      cmd_warns,      "Show warnings for a user",            False),
        ("clearwarns", cmd_clearwarns, "Clear all warnings for a user",       True),
        ("pin",        cmd_pin,        "Pin a message [loud]",                True),
        ("unpin",      cmd_unpin,      "Unpin a message",                     True),
        ("rules",      cmd_rules,      "Show group rules",                    False),
    ]
    for name, handler, desc, admin in cmds:
        registry.register_command(name, handler, desc, admin_only=admin)
        application.add_handler(CommandHandler(name, handler))
    log.info("admin module loaded")
