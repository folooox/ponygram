"""
群组管理命令。

命令（均需群管理员权限）：
/mute   [时长] — 禁止用户发言
/unmute        — 解除禁言
/kick          — 踢出用户（可重新加入）
/ban           — 永久封禁
/unban         — 解除封禁
/warn   [原因] — 警告用户（达到上限自动封禁）
/warns         — 查看用户警告记录
/clearwarns    — 清除用户所有警告
/pin   [loud]  — 置顶消息（加 loud 发送通知）
/unpin         — 取消置顶
/rules         — 查看群规

支持：回复消息 / 用户 ID / @用户名
时长格式：1h、30m、2d（不填则永久）
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram import Update, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import TelegramError
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

_NO_TARGET = "❌ 请回复某条消息，或提供用户 ID / @用户名"


def _parse_duration(text: str) -> Optional[int]:
    m = _DURATION_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)) * _DURATION_MAP[m.group(2).lower()]


def _duration_label(secs: int) -> str:
    if secs % 86400 == 0:
        return f"{secs // 86400}天"
    if secs % 3600 == 0:
        return f"{secs // 3600}小时"
    if secs % 60 == 0:
        return f"{secs // 60}分钟"
    return f"{secs}秒"


async def _resolve_target(update: Update, context) -> Tuple[Optional[int], str]:
    msg = update.effective_message
    chat = update.effective_chat
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name or str(u.id)
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
        await msg.reply_text(_NO_TARGET)
        return

    until: Optional[datetime] = None
    duration_str = ""
    for a in reversed(context.args or []):
        secs = _parse_duration(a)
        if secs:
            until = datetime.utcnow() + timedelta(seconds=secs)
            duration_str = f" {_duration_label(secs)}"
            break

    try:
        await chat.restrict_member(
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        suffix = f"（{duration_str.strip()}）" if duration_str else "（永久）"
        await msg.reply_text(
            f"🔇 已禁言 *{name}*{suffix}",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("User muted", user_id=user_id, chat_id=chat.id, until=until)
    except TelegramError as e:
        await msg.reply_text(f"❌ 禁言失败：{e}")


@group_only
@admin_only
async def cmd_unmute(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text(_NO_TARGET)
        return

    try:
        # 获取群组当前默认权限，确保完整恢复
        full_chat = await context.bot.get_chat(chat.id)
        perms = full_chat.permissions or ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        )
        await chat.restrict_member(user_id, permissions=perms)
        await msg.reply_text(
            f"🔊 已解除禁言：*{name}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("User unmuted", user_id=user_id, chat_id=chat.id)
    except TelegramError as e:
        await msg.reply_text(f"❌ 解除禁言失败：{e}")


@group_only
@admin_only
async def cmd_kick(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text(_NO_TARGET)
        return

    try:
        await chat.ban_member(user_id)
        await chat.unban_member(user_id)
        await msg.reply_text(
            f"👢 已踢出：*{name}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("User kicked", user_id=user_id, chat_id=chat.id)
    except TelegramError as e:
        await msg.reply_text(f"❌ 踢出失败：{e}")


@group_only
@admin_only
async def cmd_ban(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text(_NO_TARGET)
        return

    args = context.args or []
    reason = " ".join(args[1:] if not msg.reply_to_message and args else args)

    try:
        await chat.ban_member(user_id)
        text = f"🚫 已封禁：*{name}*"
        if reason:
            text += f"\n原因：{reason}"
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        log.info("User banned", user_id=user_id, chat_id=chat.id, reason=reason)
    except TelegramError as e:
        await msg.reply_text(f"❌ 封禁失败：{e}")


@group_only
@admin_only
async def cmd_unban(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text(_NO_TARGET)
        return

    try:
        await chat.unban_member(user_id)
        await msg.reply_text(
            f"✅ 已解封：*{name}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        log.info("User unbanned", user_id=user_id, chat_id=chat.id)
    except TelegramError as e:
        await msg.reply_text(f"❌ 解封失败：{e}")


# ---------------------------------------------------------------------------
# 警告系统
# ---------------------------------------------------------------------------

@group_only
@admin_only
async def cmd_warn(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text(_NO_TARGET)
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
        except TelegramError:
            pass
        text = f"🚫 *{name}* 警告次数达到上限（{count}/{limit}），已自动封禁"
        if reason:
            text += f"\n最后原因：{reason}"
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        log.info("User auto-banned after warn limit", user_id=user_id, chat_id=chat.id)
    else:
        text = f"⚠️ *{name}* 已被警告（{count}/{limit}）"
        if reason:
            text += f"\n原因：{reason}"
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    log.info("User warned", user_id=user_id, chat_id=chat.id, count=count)


@group_only
async def cmd_warns(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        user = update.effective_user
        if user:
            user_id, name = user.id, user.first_name or str(user.id)
        else:
            await msg.reply_text(_NO_TARGET)
            return

    warns = await get_warns(chat.id, user_id)
    settings = await get_group_settings(chat.id)
    limit = settings.warn_limit or 3

    if not warns:
        await msg.reply_text(f"✅ *{name}* 在本群没有警告记录", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"⚠️ *{name}* — {len(warns)}/{limit} 条警告："]
    for i, w in enumerate(warns, 1):
        date_str = w.warned_at.strftime("%Y-%m-%d")
        reason_str = w.reason or "（无原因）"
        lines.append(f"  {i}. {date_str}：{reason_str}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@group_only
@admin_only
async def cmd_clearwarns(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    user_id, name = await _resolve_target(update, context)
    if not user_id:
        await msg.reply_text(_NO_TARGET)
        return

    count = await clear_warns(chat.id, user_id)
    await msg.reply_text(
        f"✅ 已清除 *{name}* 的 {count} 条警告",
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info("Warnings cleared", user_id=user_id, chat_id=chat.id, count=count)


# ---------------------------------------------------------------------------
# 置顶
# ---------------------------------------------------------------------------

@group_only
@admin_only
async def cmd_pin(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    if not msg.reply_to_message:
        await msg.reply_text("❌ 请回复要置顶的消息")
        return

    loud = bool(context.args) and context.args[0].lower() == "loud"
    try:
        await context.bot.pin_chat_message(
            chat.id,
            msg.reply_to_message.message_id,
            disable_notification=not loud,
        )
        await msg.reply_text("📌 消息已置顶")
        log.info("Message pinned", chat_id=chat.id, msg_id=msg.reply_to_message.message_id)
    except TelegramError as e:
        await msg.reply_text(f"❌ 置顶失败：{e}")


@group_only
@admin_only
async def cmd_unpin(update: Update, context) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    assert msg and chat

    try:
        if msg.reply_to_message:
            await context.bot.unpin_chat_message(chat.id, msg.reply_to_message.message_id)
        else:
            await context.bot.unpin_chat_message(chat.id)
        await msg.reply_text("📌 已取消置顶")
        log.info("Message unpinned", chat_id=chat.id)
    except TelegramError as e:
        await msg.reply_text(f"❌ 取消置顶失败：{e}")


async def cmd_rules(update: Update, context) -> None:
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
        ("mute",       cmd_mute,       "禁言用户 [时长: 1h/30m/2d]",   True),
        ("unmute",     cmd_unmute,     "解除禁言",                       True),
        ("kick",       cmd_kick,       "踢出用户（可重新加入）",          True),
        ("ban",        cmd_ban,        "永久封禁用户",                   True),
        ("unban",      cmd_unban,      "解除封禁",                       True),
        ("warn",       cmd_warn,       "警告用户（达上限自动封禁）",      True),
        ("warns",      cmd_warns,      "查看用户警告记录",               False),
        ("clearwarns", cmd_clearwarns, "清除用户所有警告",               True),
        ("pin",        cmd_pin,        "置顶消息 [loud]",               True),
        ("unpin",      cmd_unpin,      "取消置顶",                       True),
        ("rules",      cmd_rules,      "查看群规则",                     False),
    ]
    for name, handler, desc, admin in cmds:
        registry.register_command(name, handler, desc, admin_only=admin)
        application.add_handler(CommandHandler(name, handler))
    log.info("admin module loaded")
