"""
Welcome / goodbye messages and join verification.

Features
--------
- Configurable welcome message per group (supports {name}, {username}, {chat} placeholders)
- Goodbye message on member leave
- Join verification: new member must click a button within a timeout or get kicked
- Admin commands: /setwelcome, /delwelcome, /setgoodbye, /delgoodbye,
                  /verification on|off, /verificationtimeout <seconds>
"""

from __future__ import annotations

import asyncio
from typing import Dict, Tuple

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
)

from bot.database import get_group_settings, set_group_field, upsert_user
from bot.logger import get_logger
from bot.permissions import admin_only, group_only
from bot.router import registry

log = get_logger(__name__)

_DEFAULT_WELCOME = (
    "👋 Welcome, {name}! Glad to have you in *{chat}*.\n"
    "Please read the group rules before chatting."
)
_DEFAULT_GOODBYE = "👋 {name} has left the group. Goodbye!"

# pending_verifications: chat_id -> {user_id: (msg_id, asyncio.Task)}
_pending: Dict[int, Dict[int, Tuple[int, asyncio.Task]]] = {}


def _format(template: str, user, chat) -> str:
    name = user.first_name or "User"
    if user.last_name:
        name += f" {user.last_name}"
    username = f"@{user.username}" if user.username else name
    return template.format(
        name=name,
        username=username,
        chat=chat.title or "this group",
        id=user.id,
    )


async def _kick_unverified(app, chat_id: int, user_id: int, msg_id: int) -> None:
    """Called after verification timeout — kicks the user and deletes the prompt."""
    try:
        await app.bot.ban_chat_member(chat_id, user_id)
        await app.bot.unban_chat_member(chat_id, user_id)
        await app.bot.delete_message(chat_id, msg_id)
        log.info("Kicked unverified user", user_id=user_id, chat_id=chat_id)
    except Exception as e:
        log.warning("Failed to kick unverified user", error=str(e))
    finally:
        _pending.get(chat_id, {}).pop(user_id, None)


async def on_chat_member(update: Update, context) -> None:
    """Handle member join / leave events."""
    result = update.chat_member
    if not result:
        return

    chat = result.chat
    new_status = result.new_chat_member
    old_status = result.old_chat_member
    user = new_status.user

    if user.is_bot:
        return

    await upsert_user(user)
    settings = await get_group_settings(chat.id)

    # ---- Member joined ----
    just_joined = (
        old_status.status in ("left", "kicked", "restricted")
        and new_status.status == "member"
    )
    if just_joined:
        if settings.verification_enabled:
            await _start_verification(context.application, chat, user, settings)
        elif settings.welcome_enabled:
            text = _format(
                settings.welcome_text or _DEFAULT_WELCOME, user, chat
            )
            await context.bot.send_message(
                chat.id, text, parse_mode=ParseMode.MARKDOWN
            )
        return

    # ---- Member left / kicked ----
    just_left = (
        old_status.status == "member"
        and new_status.status in ("left", "kicked")
    )
    if just_left and settings.goodbye_enabled:
        text = _format(settings.goodbye_text or _DEFAULT_GOODBYE, user, chat)
        await context.bot.send_message(chat.id, text, parse_mode=ParseMode.MARKDOWN)


async def _start_verification(app, chat, user, settings) -> None:
    """Mute new user and send a verification button."""
    try:
        await app.bot.restrict_member(
            chat.id,
            user.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
    except BadRequest:
        pass

    name = user.first_name or "User"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ I'm not a bot — click to verify",
            callback_data=f"verify:{chat.id}:{user.id}",
        )
    ]])
    sent = await app.bot.send_message(
        chat.id,
        f"👋 Welcome, [{name}](tg://user?id={user.id})!\n"
        f"Please verify you're human within {settings.verification_timeout} seconds "
        f"by clicking the button below, or you'll be removed.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )

    timeout = settings.verification_timeout

    async def _timeout_task():
        await asyncio.sleep(timeout)
        await _kick_unverified(app, chat.id, user.id, sent.message_id)

    task = asyncio.create_task(_timeout_task())
    _pending.setdefault(chat.id, {})[user.id] = (sent.message_id, task)


async def on_verify_button(update: Update, context) -> None:
    """Handle verification button press."""
    query = update.callback_query
    assert query
    _, chat_id_str, user_id_str = query.data.split(":")
    chat_id, user_id = int(chat_id_str), int(user_id_str)

    if query.from_user.id != user_id:
        await query.answer("This button is not for you.", show_alert=True)
        return

    pending = _pending.get(chat_id, {}).pop(user_id, None)
    if pending:
        msg_id, task = pending
        task.cancel()
        try:
            await query.message.delete()
        except BadRequest:
            pass

    # Restore default group permissions
    chat = await context.bot.get_chat(chat_id)
    try:
        await context.bot.restrict_member(
            chat_id,
            user_id,
            permissions=chat.permissions or ChatPermissions(
                can_send_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
            ),
        )
    except BadRequest:
        pass

    settings = await get_group_settings(chat_id)
    text = _format(settings.welcome_text or _DEFAULT_WELCOME, query.from_user, chat)
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    await query.answer("✅ Verified! Welcome!")
    log.info("User verified", user_id=user_id, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Admin configuration commands
# ---------------------------------------------------------------------------

@group_only
@admin_only
async def cmd_setwelcome(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    if not context.args:
        await msg.reply_text(
            "Usage: /setwelcome <text>\n"
            "Placeholders: {name} {username} {chat} {id}"
        )
        return
    text = " ".join(context.args)
    await set_group_field(chat.id, welcome_text=text, welcome_enabled=True)
    await msg.reply_text("✅ Welcome message updated.")


@group_only
@admin_only
async def cmd_delwelcome(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    await set_group_field(chat.id, welcome_text=None, welcome_enabled=False)
    await msg.reply_text("✅ Welcome message disabled.")


@group_only
@admin_only
async def cmd_setgoodbye(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    if not context.args:
        await msg.reply_text("Usage: /setgoodbye <text>")
        return
    text = " ".join(context.args)
    await set_group_field(chat.id, goodbye_text=text, goodbye_enabled=True)
    await msg.reply_text("✅ Goodbye message updated.")


@group_only
@admin_only
async def cmd_delgoodbye(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    await set_group_field(chat.id, goodbye_text=None, goodbye_enabled=False)
    await msg.reply_text("✅ Goodbye message disabled.")


@group_only
@admin_only
async def cmd_verification(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await msg.reply_text("Usage: /verification on|off")
        return
    enabled = context.args[0].lower() == "on"
    await set_group_field(chat.id, verification_enabled=enabled)
    state = "enabled" if enabled else "disabled"
    await msg.reply_text(f"✅ Join verification {state}.")


@group_only
@admin_only
async def cmd_vertimeout(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("Usage: /verificationtimeout <seconds>")
        return
    secs = max(10, min(int(context.args[0]), 600))
    await set_group_field(chat.id, verification_timeout=secs)
    await msg.reply_text(f"✅ Verification timeout set to {secs} seconds.")


def setup(application: Application) -> None:
    application.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(on_verify_button, pattern=r"^verify:"))

    cmds = [
        ("setwelcome",           cmd_setwelcome,  "Set welcome message",              True),
        ("delwelcome",           cmd_delwelcome,  "Disable welcome message",          True),
        ("setgoodbye",           cmd_setgoodbye,  "Set goodbye message",              True),
        ("delgoodbye",           cmd_delgoodbye,  "Disable goodbye message",          True),
        ("verification",         cmd_verification,"Toggle join verification on/off",  True),
        ("verificationtimeout",  cmd_vertimeout,  "Set verification timeout (secs)",  True),
    ]
    for name, handler, desc, admin in cmds:
        registry.register_command(name, handler, desc, admin_only=admin)
        application.add_handler(CommandHandler(name, handler))
    log.info("welcome module loaded")
