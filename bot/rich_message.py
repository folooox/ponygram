"""
Bot API 10.1 Rich Messages — raw-API helpers.

python-telegram-bot has no wrappers for the Bot API 10.1 methods
``sendRichMessage`` / ``sendRichMessageDraft`` (added 2026-06-11), so we POST
them directly to the HTTP API at :pyattr:`telegram.Bot.base_url`.

The ``InputRichMessage`` object accepts a server-parsed ``markdown`` field
(verified against the live API), so we never build a ``RichText`` entity tree by
hand — we hand Telegram Markdown and it renders headings, lists, tables, code,
quotes, etc. natively.

Every call is **best-effort**: any failure — an older Bot API server returning
``404 Not Found`` for the unknown method, a payload rejection, or a transport
error — is swallowed and reported as ``False`` so callers fall back to their
existing send path. Nothing here raises.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from bot.logger import get_logger

log = get_logger(__name__)

# Telegram's documented rich-text size ceiling (bytes).
_MAX_RICH_BYTES = 32_768

_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    """Lazily create a shared async HTTP client on the running loop."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return _client


def _build_input_rich_message(text: str) -> dict:
    """Build an ``InputRichMessage`` from Markdown text.

    Telegram parses the ``markdown`` field server-side into the rich document
    model (headings, lists, tables, code, quotes, …)."""
    return {"markdown": text}


def _too_big(text: str) -> bool:
    return len(text.encode("utf-8")) > _MAX_RICH_BYTES


async def _raw(bot, method: str, payload: dict) -> Optional[Any]:
    """POST ``method`` to the Bot API.

    Return the ``result`` field on success, or ``None`` on any failure (logged
    at debug level, never raised)."""
    try:
        resp = await _http().post(f"{bot.base_url}/{method}", json=payload)
        data: dict[str, Any] = resp.json()
    except Exception as e:  # transport / JSON decode
        log.debug("rich api transport error", method=method, error=str(e))
        return None
    if not data.get("ok"):
        log.debug(
            "rich api rejected",
            method=method,
            code=data.get("error_code"),
            desc=data.get("description"),
        )
        return None
    return data.get("result")


async def send_rich(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_to: Optional[int] = None,
    reply_markup: Any = None,
) -> bool:
    """Send a final rich message (works in all chat types).

    Returns ``True`` on success; ``False`` means the caller should fall back to
    its legacy send path (older server, oversize text, or a rejected payload)."""
    if not text or _too_big(text):
        return False
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _build_input_rich_message(text),
    }
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await _raw(bot, "sendRichMessage", payload) is not None


async def send_rich_draft(bot, chat_id: int, text: str) -> bool:
    """Stream a partial rich message as an animated draft preview.

    DM-only — the caller must guard on chat type (groups have no draft path).
    Drafts are ephemeral: this returns a Boolean, not a Message, so there is
    nothing to edit; push successive partials, then post the real message with
    :func:`send_rich`. Returns ``True`` on success."""
    if not text or _too_big(text):
        return False
    payload = {
        "chat_id": chat_id,
        "rich_message": _build_input_rich_message(text),
    }
    return await _raw(bot, "sendRichMessageDraft", payload) is not None
