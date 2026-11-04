"""Outbound Telegram notify target stored in bot_config.

Web Admin edits apply on the next send. No second poller is started: delivery
uses the already-running bot's send_message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from telegram.error import TelegramError

from bot.database import BotConfig, get_bot_config, get_session
from bot.logger import get_logger

log = get_logger(__name__)

NOTIFY_CHAT_ID_KEY = "notify_chat_id"
NOTIFY_LABEL_KEY = "notify_label"
# Seeded only when the row has never been stored.
DEFAULT_NOTIFY_CHAT_ID = "-1003998603559"
TEST_PING = "Ponygram 通知测试"
_CHAT_ID = re.compile(r"^-?[1-9]\d{0,18}$")
_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_INT64_MIN = -2**63
_INT64_MAX = 2**63 - 1
_bot: Any = None


class NotifyError(Exception):
    """Destination missing or the running bot could not deliver the text."""


@dataclass(frozen=True)
class NotifyDestination:
    chat_id: int
    label: str


def bind(bot: Any) -> None:
    """Remember the process bot so callers can send without threading it through."""
    global _bot
    _bot = bot


def parse_chat_id(value: object) -> int:
    """Accept a Telegram chat id: digits, optional leading minus. Reject @names and 0."""
    text = str(value or "").strip()
    if not _CHAT_ID.fullmatch(text):
        raise ValueError("Chat ID 必须是整数，频道或超级群可以是负数。")
    number = int(text)
    if number < _INT64_MIN or number > _INT64_MAX:
        raise ValueError("Chat ID 超出 Telegram 允许的整数范围。")
    return number


def normalize_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:128]


async def ensure_notify_config() -> None:
    """Insert the default chat id once. Later edits, including a cleared row, stay put."""
    async with get_session() as session:
        row = await session.get(BotConfig, NOTIFY_CHAT_ID_KEY)
        if row is not None:
            return
        session.add(BotConfig(key=NOTIFY_CHAT_ID_KEY, value=DEFAULT_NOTIFY_CHAT_ID))
        await session.commit()


async def get_destination() -> NotifyDestination:
    raw = await get_bot_config(NOTIFY_CHAT_ID_KEY)
    if not raw:
        raise NotifyError("未配置通知 Chat ID。")
    try:
        chat_id = parse_chat_id(raw)
    except ValueError as exc:
        raise NotifyError(str(exc)) from exc
    label = await get_bot_config(NOTIFY_LABEL_KEY) or ""
    return NotifyDestination(chat_id=chat_id, label=label)


def _public_error(exc: BaseException) -> str:
    text = _TOKEN.sub("[redacted]", str(exc)).replace("\n", " ").strip()
    return (text or type(exc).__name__)[:180]


async def send_text(
    text: str,
    *,
    bot: Any = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
) -> int:
    """Send plain text to the configured chat. Returns the Telegram message id.

    Raises NotifyError when the chat id is missing or invalid, no bot is
    connected, the text cannot be sent, or Telegram rejects it. Does not open
    another getUpdates session.
    """
    if not isinstance(text, str) or not text.strip():
        raise NotifyError("通知内容为空。")
    if len(text) > 4096:
        raise NotifyError("通知内容超过 Telegram 4096 字限制。")
    destination = await get_destination()
    client = bot if bot is not None else _bot
    if client is None or not hasattr(client, "send_message"):
        raise NotifyError("机器人尚未连接，无法发送通知。")
    try:
        sent = await client.send_message(
            destination.chat_id,
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramError as exc:
        log.warning("notify send failed", chat_id=destination.chat_id, error_type=type(exc).__name__)
        raise NotifyError("通知发送失败：" + _public_error(exc)) from exc
    return int(getattr(sent, "message_id", 0) or 0)
