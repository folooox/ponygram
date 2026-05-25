"""
User info commands: /id and /whois.

/id          — show your own Telegram ID and basic info
/id @user    — show another user's ID (reply to a message also works)
/whois       — same as /id but with more detail from the DB
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.constants import ParseMode

from bot.database import get_user, upsert_user
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)


def _user_mention(user) -> str:
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return f"[{name}](tg://user?id={user.id})"


async def cmd_id(update: Update, context) -> None:
    """Show Telegram ID of yourself or a replied-to user."""
    msg = update.effective_message
    assert msg

    # Priority: replied-to message > command argument (not supported for IDs) > self
    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    else:
        target = update.effective_user

    if not target:
        await msg.reply_text("Could not determine target user.")
        return

    await upsert_user(target)

    chat = update.effective_chat
    lines = [
        f"👤 *User info*",
        f"• ID: `{target.id}`",
        f"• Name: {_user_mention(target)}",
    ]
    if target.username:
        lines.append(f"• Username: @{target.username}")
    if chat and chat.type in ("group", "supergroup"):
        try:
            member = await chat.get_member(target.id)
            lines.append(f"• Status: {member.status}")
        except Exception:
            pass

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_whois(update: Update, context) -> None:
    """Show detailed info from the bot's database for a user."""
    msg = update.effective_message
    assert msg

    target_tg = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_tg = msg.reply_to_message.from_user
    else:
        target_tg = update.effective_user

    if not target_tg:
        await msg.reply_text("Could not determine target user.")
        return

    await upsert_user(target_tg)
    db_user = await get_user(target_tg.id)

    lines = [f"🔍 *Whois: {_user_mention(target_tg)}*", f"• ID: `{target_tg.id}`"]
    if target_tg.username:
        lines.append(f"• Username: @{target_tg.username}")
    if db_user:
        lines.append(f"• First seen: {db_user.first_seen.strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append(f"• Last seen: {db_user.last_seen.strftime('%Y-%m-%d %H:%M UTC')}")
        if db_user.language_code:
            lines.append(f"• Language: {db_user.language_code}")

    from bot.database import is_blacklisted
    if await is_blacklisted(target_tg.id):
        lines.append("• ⛔ Globally blacklisted")

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def setup(application: Application) -> None:
    registry.register_command("id", cmd_id, "Show user ID and info")
    registry.register_command("whois", cmd_whois, "Show detailed user info")
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("whois", cmd_whois))
    log.info("userinfo module loaded")
