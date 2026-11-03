"""
FilmSpot channel drafts.

Owner-only private-chat flow: search TMDB (or paste a freeform note), preview a
fixed Chinese template, then confirm before anything is posted.

The destination channel is ``filmspot_channel_id`` in bot_config (Web Admin →
组件 → FilmSpot). It is empty until an admin saves one. Preview works with no
channel configured; 「发送到频道」 explains how to set it and does not post.

Publishing uses the running Application bot (``context.bot``). There is no
second poller.
"""

from __future__ import annotations

import hashlib
import html
import re
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.components import (
    Component,
    ConfigField,
    is_module_enabled,
    register_component,
    require_module,
)
from bot.logger import get_logger
from bot.permissions import owner_only
from bot.router import registry

log = get_logger(__name__)

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4000
DRAFT_TTL = 3 * 60 * 60
WAIT_TTL = 10 * 60
_IMG_W780 = "https://image.tmdb.org/t/p/w780"

_OFF = {"0", "false", "off", "no", "否"}
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_TME = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/?",
    re.IGNORECASE,
)
_BULLET_PREFIX = re.compile(r"^[•\-\*·]+|^[\d]+[.、]\s*")

_USAGE = (
    "频道稿只在私聊里预览，确认后才会发到已配置的频道。\n\n"
    "/filmspot <片名> — 按电影搜索\n"
    "/filmspot movie <片名>\n"
    "/filmspot tv <剧名>\n"
    "/channel_post movie|tv <名称>\n"
    "/gossip — 花边（下一条消息为正文，可带一张图）\n"
    "/channel_text — 同 /gossip\n\n"
    "频道在 Web 管理面板 → 组件 → FilmSpot 里填写 filmspot_channel_id。"
    "留空时可以预览，不能发送。"
)


def utf16_len(text: str) -> int:
    """Telegram counts caption length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def parse_channel_id(raw: Optional[str]) -> int | str | None:
    """Return a chat id suitable for Bot API, or None when unset/invalid.

    Accepts a numeric id (including negative channel ids) or a public
    ``@username`` / t.me link. Never invents a destination.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    link = _TME.fullmatch(text)
    if link:
        return "@" + link.group(1)
    name = text[1:] if text.startswith("@") else text
    if _USERNAME.fullmatch(name):
        return "@" + name
    return None


def publishing_enabled(stored: Optional[str]) -> bool:
    """Unset means on. Only an explicit off value blocks sending."""
    return (stored or "").strip().lower() not in _OFF


def publishing_block_reason(enabled: Optional[str], channel: Optional[str]) -> Optional[str]:
    """Chinese explanation when a confirm must not post, else None."""
    reasons: list[str] = []
    if not publishing_enabled(enabled):
        reasons.append(
            "频道发布已关闭。请在 Web 管理面板 → 组件 → FilmSpot 打开「启用频道发布」。"
        )
    raw = (channel or "").strip()
    parsed = parse_channel_id(raw)
    if parsed is None and not raw:
        reasons.append(
            "尚未配置发布频道。请在 Web 管理面板 → 组件 → FilmSpot 填写 "
            "filmspot_channel_id（频道数字 ID 或 @username），并确认机器人是该频道的管理员。"
        )
    elif parsed is None:
        reasons.append(
            "filmspot_channel_id 无法识别。请改成频道数字 ID 或 @username。"
        )
    if not reasons:
        return None
    return "还不能发到频道。\n" + "\n".join(reasons)


def poster_url_from_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    text = str(path).strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if not text.startswith("/"):
        text = "/" + text
    return _IMG_W780 + text


def _hashtag(label: str) -> str:
    cleaned = re.sub(r"[^\w]+", "", label or "", flags=re.UNICODE).strip("_")
    return cleaned


def derive_selling_points(detail: dict) -> list[str]:
    """One to three short bullets from tagline, score, and popularity."""
    points: list[str] = []
    title = (detail.get("title") or detail.get("name") or "").strip()
    tagline = (detail.get("tagline") or "").strip()
    if tagline and tagline != title:
        points.append(tagline)
    try:
        vote = float(detail.get("vote_average") or 0)
    except (TypeError, ValueError):
        vote = 0.0
    try:
        votes = int(detail.get("vote_count") or 0)
    except (TypeError, ValueError):
        votes = 0
    if vote > 0 and votes >= 20 and len(points) < 3:
        points.append(f"评分 {vote:.1f}/10（{votes:,} 人）")
    try:
        popularity = float(detail.get("popularity") or 0)
    except (TypeError, ValueError):
        popularity = 0.0
    if popularity >= 20 and len(points) < 3:
        points.append(f"热度 {popularity:.0f}")
    return points[:3]


def apply_selling_lines(text: str) -> list[str]:
    """Take up to three non-empty lines, stripping a leading bullet."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = _BULLET_PREFIX.sub("", raw.strip()).strip()
        if not line:
            continue
        lines.append(line)
        if len(lines) == 3:
            break
    return lines


def merge_media(item: Optional[dict], detail: Optional[dict]) -> dict:
    """Overlay a details payload onto a search hit. Empty detail fields lose."""
    merged = dict(item or {})
    if not detail:
        return merged
    for key, value in detail.items():
        if value is None or value == [] or value == {}:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    item_overview = ((item or {}).get("overview") or "").strip()
    if not ((detail.get("overview") or "").strip()) and item_overview:
        merged["overview"] = item_overview
    return merged


def _people(
    rows: Optional[list],
    *,
    limit: int,
    jobs: Optional[set[str]] = None,
) -> list[str]:
    names: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if jobs and row.get("job") not in jobs:
            continue
        name = (row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def parts_from_detail(detail: dict, media_type: str) -> dict:
    """Normalize a TMDB movie/TV object into template fields."""
    media_type = "tv" if media_type == "tv" else "movie"
    title = (detail.get("title") or detail.get("name") or "未知").strip() or "未知"
    original = (detail.get("original_title") or detail.get("original_name") or "").strip()
    genres: list[str] = []
    for genre in detail.get("genres") or []:
        if isinstance(genre, dict):
            name = (genre.get("name") or "").strip()
        else:
            name = str(genre).strip()
        if name and name not in genres:
            genres.append(name)
    credits = detail.get("credits") or {}
    if media_type == "tv":
        creators = _people(detail.get("created_by"), limit=3)
        if not creators:
            creators = _people(credits.get("crew"), limit=3, jobs={"Director", "导演"})
        if not creators:
            creators = _people(
                credits.get("crew"), limit=2, jobs={"Executive Producer"},
            )
    else:
        creators = _people(credits.get("crew"), limit=3, jobs={"Director", "导演"})
    cast_rows = credits.get("cast") or []
    if not cast_rows and media_type == "tv":
        cast_rows = (detail.get("aggregate_credits") or {}).get("cast") or []
    cast = _people(cast_rows, limit=3)
    if media_type == "movie":
        date = str(detail.get("release_date") or "").strip()
    else:
        date = str(detail.get("first_air_date") or "").strip()
    overview = (detail.get("overview") or "").strip() or "暂无梗概。"
    return {
        "media_type": media_type,
        "title": title,
        "original": original,
        "genres": genres,
        "creators": creators,
        "cast": cast,
        "date": date,
        "overview": overview,
        "selling": derive_selling_points(detail),
        "poster_path": detail.get("poster_path") or "",
    }


def _creator_line(parts: dict) -> str:
    bits: list[str] = []
    creators = parts.get("creators") or []
    cast = parts.get("cast") or []
    if creators:
        role = "导演" if parts.get("media_type") == "movie" else "创作者"
        bits.append(role + " " + "、".join(_esc(name) for name in creators))
    if cast:
        bits.append("主演 " + "、".join(_esc(name) for name in cast))
    return " · ".join(bits) if bits else "资料待补"


def _tags(parts: dict) -> str:
    tags: list[str] = []
    for genre in parts.get("genres") or []:
        token = _hashtag(genre)
        if token:
            tag = "#" + token
            if tag not in tags:
                tags.append(tag)
    media = "#电影" if parts.get("media_type") == "movie" else "#剧集"
    if media not in tags:
        tags.append(media)
    if "#FilmSpot" not in tags:
        tags.append("#FilmSpot")
    return " ".join(tags)


def _selling_block(points: Optional[list]) -> str:
    rows = [p for p in (points or []) if str(p).strip()][:3]
    if not rows:
        rows = ["（待补充卖点，可点「改卖点」修改）"]
    return "\n".join("• " + _esc(p) for p in rows)


def _body(parts: dict, overview: str, include_tail: bool) -> str:
    icon = "📺" if parts.get("media_type") == "tv" else "🎬"
    lines = [f"{icon} <b>片名</b>：{_esc(parts.get('title') or '未知')}"]
    original = (parts.get("original") or "").strip()
    title = (parts.get("title") or "").strip()
    if original and original != title:
        lines.append(f"<b>原名</b>：{_esc(original)}")
    genres = parts.get("genres") or []
    genre_text = " / ".join(_esc(g) for g in genres) if genres else "类型待补"
    lines.append(f"<b>类型</b>：{genre_text}")
    lines.append(f"<b>主创</b>：{_creator_line(parts)}")
    date = (parts.get("date") or "").strip()
    lines.append(f"<b>档期</b>：{_esc(date) if date else '档期待定'}")
    lines.append("")
    lines.append("<b>梗概</b>")
    lines.append(_esc(overview) if overview else "暂无梗概。")
    if include_tail:
        lines.append("")
        lines.append("<b>卖点</b>")
        lines.append(_selling_block(parts.get("selling")))
        lines.append("")
        lines.append("<b>标签</b>")
        lines.append(_tags(parts))
    return "\n".join(lines)


def _fit_overview(parts: dict, overview: str, include_tail: bool) -> Optional[str]:
    full = _body(parts, overview, include_tail)
    if utf16_len(full) <= CAPTION_LIMIT:
        return full
    lo, hi = 0, len(overview)
    best: Optional[str] = None
    while lo <= hi:
        mid = (lo + hi) // 2
        snippet = overview[:mid].rstrip()
        if mid < len(overview):
            snippet = (snippet + "…") if snippet else "…"
        candidate = _body(parts, snippet, include_tail)
        if utf16_len(candidate) <= CAPTION_LIMIT:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _trim_to_limit(text: str, limit: int) -> str:
    if utf16_len(text) <= limit:
        return text
    lo, hi = 0, len(text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if utf16_len(text[:mid]) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    chunk = text[:best]
    newline = chunk.rfind("\n")
    if newline > len(chunk) // 2:
        chunk = chunk[:newline]
    return chunk


def render_filmspot(parts: dict) -> tuple[str, str]:
    """Return ``(caption, followup)``. Caption is always within Telegram's limit.

    Follow-up is empty when the whole template fits. When the header plus
    selling points cannot fit beside the synopsis, selling points and tags
    move to the follow-up message.
    """
    overview = (parts.get("overview") or "").strip() or "暂无梗概。"
    fitted = _fit_overview(parts, overview, include_tail=True)
    if fitted is not None:
        return fitted, ""
    head = _fit_overview(parts, overview, include_tail=False)
    if head is None:
        head = _trim_to_limit(_body(parts, "…", False), CAPTION_LIMIT)
    elif utf16_len(head) > CAPTION_LIMIT:
        head = _trim_to_limit(head, CAPTION_LIMIT)
    tail = "\n".join([
        "<b>卖点</b>",
        _selling_block(parts.get("selling")),
        "",
        "<b>标签</b>",
        _tags(parts),
    ])
    return head, tail


def render_gossip(text: str) -> tuple[str, str]:
    """Freeform note. Long text is split so a photo caption still fits."""
    body = (text or "").strip() or "（空）"
    header = "🗞 <b>花边</b>\n\n"
    footer = "\n\n#花边 #FilmSpot"
    full = header + _esc(body) + footer
    if utf16_len(full) <= CAPTION_LIMIT:
        return full, ""
    lo, hi = 0, len(body)
    best_i = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        snippet = body[:mid].rstrip()
        caption = header + _esc(snippet) + ("…" if mid < len(body) else "")
        if utf16_len(caption) <= CAPTION_LIMIT:
            best_i = mid
            lo = mid + 1
        else:
            hi = mid - 1
    snippet = body[:best_i].rstrip()
    caption = header + _esc(snippet) + ("…" if best_i < len(body) else "")
    if utf16_len(caption) > CAPTION_LIMIT:
        caption = _trim_to_limit(caption, CAPTION_LIMIT)
    rest = body[best_i:].strip()
    follow = (_esc(rest) + footer) if rest else footer.strip()
    return caption, follow


def message_chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    text = text or ""
    if not text:
        return []
    if utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if utf16_len(rest) <= limit:
            chunks.append(rest)
            break
        # Cut by UTF-16 length, preferring a newline boundary.
        lo, hi = 0, len(rest)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if mid and utf16_len(rest[:mid]) <= limit:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        window = rest[:best]
        cut = window.rfind("\n")
        if cut < best // 2:
            cut = best
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks


def confirm_keyboard(token: str, *, with_selling: bool) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("发送到频道", callback_data=f"fs_send:{token}"),
        InlineKeyboardButton("取消", callback_data=f"fs_cancel:{token}"),
    ]]
    if with_selling:
        rows.append([
            InlineKeyboardButton("改卖点", callback_data=f"fs_sell:{token}"),
        ])
    return InlineKeyboardMarkup(rows)


@dataclass
class Draft:
    token: str
    owner_id: int
    kind: str
    caption: str
    followup: str
    poster_url: Optional[str] = None
    photo_file_id: Optional[str] = None
    parts: Optional[dict] = None
    expires: float = 0.0
    inflight: bool = False


@dataclass
class _Wait:
    kind: str
    token: str
    expires: float


_DRAFTS: dict[str, Draft] = {}
_WAITING: dict[int, _Wait] = {}


def _purge(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    for token, draft in list(_DRAFTS.items()):
        if draft.expires <= now:
            _DRAFTS.pop(token, None)
    for user_id, wait in list(_WAITING.items()):
        if wait.expires <= now:
            _WAITING.pop(user_id, None)
    if len(_DRAFTS) > 80:
        oldest = sorted(_DRAFTS.values(), key=lambda d: d.expires)[: len(_DRAFTS) - 80]
        for draft in oldest:
            _DRAFTS.pop(draft.token, None)


def _new_token() -> str:
    return secrets.token_hex(4)


def _store(draft: Draft) -> Draft:
    _purge()
    draft.expires = time.monotonic() + DRAFT_TTL
    _DRAFTS[draft.token] = draft
    return draft


def _drop_draft(token: str) -> None:
    _DRAFTS.pop(token, None)
    for user_id, wait in list(_WAITING.items()):
        if wait.token == token:
            _WAITING.pop(user_id, None)


def _is_owner(update: Update, context) -> bool:
    user = update.effective_user
    cfg = context.bot_data.get("config") if context else None
    return bool(user and cfg and cfg.is_owner(user.id))


def _clear_wait(user_id: Optional[int]) -> None:
    if user_id is not None:
        _WAITING.pop(user_id, None)


async def _deliver(bot, chat_id: int, draft: Draft, *, preview: bool) -> None:
    markup = confirm_keyboard(draft.token, with_selling=bool(draft.parts)) if preview else None
    photo = draft.photo_file_id or draft.poster_url
    followups = message_chunks(draft.followup)
    if photo:
        photo_kwargs = {
            "caption": draft.caption,
            "parse_mode": ParseMode.HTML,
        }
        if markup and not followups:
            photo_kwargs["reply_markup"] = markup
        await bot.send_photo(chat_id, photo, **photo_kwargs)
        for index, part in enumerate(followups):
            await bot.send_message(
                chat_id,
                part,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup if index == len(followups) - 1 else None,
            )
        return
    body = draft.caption if not draft.followup else f"{draft.caption}\n\n{draft.followup}"
    parts = message_chunks(body)
    for index, part in enumerate(parts):
        await bot.send_message(
            chat_id,
            part,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup if index == len(parts) - 1 else None,
        )


async def _publish(bot, chat_id: int | str, draft: Draft) -> None:
    """Send the same caption the owner previewed. No keyboard."""
    photo = draft.photo_file_id or draft.poster_url
    followups = message_chunks(draft.followup)
    if photo:
        await bot.send_photo(
            chat_id,
            photo,
            caption=draft.caption,
            parse_mode=ParseMode.HTML,
        )
        for part in followups:
            await bot.send_message(
                chat_id,
                part,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return
    body = draft.caption if not draft.followup else f"{draft.caption}\n\n{draft.followup}"
    for part in message_chunks(body):
        await bot.send_message(
            chat_id,
            part,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


def _make_film_draft(owner_id: int, parts: dict) -> Draft:
    caption, followup = render_filmspot(parts)
    return _store(Draft(
        token=_new_token(),
        owner_id=owner_id,
        kind="film",
        caption=caption,
        followup=followup,
        poster_url=poster_url_from_path(parts.get("poster_path")),
        parts=parts,
    ))


def _make_gossip_draft(owner_id: int, text: str, photo_file_id: Optional[str]) -> Draft:
    caption, followup = render_gossip(text)
    return _store(Draft(
        token=_new_token(),
        owner_id=owner_id,
        kind="gossip",
        caption=caption,
        followup=followup,
        photo_file_id=photo_file_id,
        parts=None,
    ))


async def fetch_detail(media_type: str, tmdb_id: int, context) -> Optional[dict]:
    """Details + credits. Prefer zh-CN copy; fill gaps from en-US."""
    from modules.movie import _get_api_key, _tmdb_get

    api_key = await _get_api_key(context)
    if not api_key:
        return None
    extra = "credits,aggregate_credits" if media_type == "tv" else "credits"
    zh = await _tmdb_get(
        f"/{media_type}/{tmdb_id}",
        api_key,
        language="zh-CN",
        append_to_response=extra,
    )
    if not zh:
        return None
    credits = zh.get("credits") or {}
    needs_en = (
        not (zh.get("overview") or "").strip()
        or not (zh.get("tagline") or "").strip()
        or not zh.get("genres")
        or not credits.get("cast")
    )
    if media_type == "movie" and not any(
        (c.get("job") in ("Director", "导演")) for c in (credits.get("crew") or [])
    ):
        needs_en = True
    if media_type == "tv" and not zh.get("created_by"):
        needs_en = True
    if not needs_en:
        return zh
    en = await _tmdb_get(
        f"/{media_type}/{tmdb_id}",
        api_key,
        language="en-US",
        append_to_response=extra,
    )
    if not en:
        return zh
    for key in ("overview", "tagline"):
        if not (zh.get(key) or "").strip() and (en.get(key) or "").strip():
            zh[key] = en[key]
    if not zh.get("genres") and en.get("genres"):
        zh["genres"] = en["genres"]
    if media_type == "tv" and not zh.get("created_by") and en.get("created_by"):
        zh["created_by"] = en["created_by"]
    en_credits = en.get("credits") or {}
    zh_credits = dict(zh.get("credits") or {})
    if not zh_credits.get("cast") and en_credits.get("cast"):
        zh_credits["cast"] = en_credits["cast"]
    if not zh_credits.get("crew") and en_credits.get("crew"):
        zh_credits["crew"] = en_credits["crew"]
    if zh_credits:
        zh["credits"] = zh_credits
    if media_type == "tv" and not (zh.get("aggregate_credits") or {}).get("cast"):
        if (en.get("aggregate_credits") or {}).get("cast"):
            zh["aggregate_credits"] = en["aggregate_credits"]
    return zh


async def build_parts(media_type: str, item: dict, context) -> dict:
    """Fetch details when the search hit has no genres/credits. Raises LookupError."""
    from modules.movie import _get_api_key

    if not await _get_api_key(context):
        raise LookupError("no_key")
    detail = None
    raw_id = item.get("id")
    if raw_id is not None:
        try:
            detail = await fetch_detail(media_type, int(raw_id), context)
        except (TypeError, ValueError):
            detail = None
    merged = merge_media(item, detail)
    if not (merged.get("title") or merged.get("name")):
        raise LookupError("failed")
    return parts_from_detail(merged, media_type)


async def _reply(update: Update, text: str) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text)


async def _need_private(update: Update) -> bool:
    chat = update.effective_chat
    if chat and chat.type == "private":
        return True
    await _reply(update, "请在与 Bot 的私聊中使用。频道稿预览只发给你，不会出现在群里。")
    return False


async def _blocked(update: Update, context) -> bool:
    """True when this callback should stop. Answers the query itself."""
    query = update.callback_query
    if not _is_owner(update, context):
        if query:
            await query.answer("仅 Bot 所有者可使用", show_alert=True)
        return True
    if not await is_module_enabled("filmspot"):
        if query:
            await query.answer("该功能已停用", show_alert=True)
        return True
    return False


async def _show_draft(update: Update, context, draft: Draft, *, prefer_private: bool) -> None:
    user = update.effective_user
    chat = update.effective_chat
    assert user is not None
    destination = user.id
    in_private = bool(chat and chat.type == "private")
    if in_private:
        destination = chat.id
    query = update.callback_query
    try:
        await _deliver(context.bot, destination, draft, preview=True)
    except TelegramError as exc:
        log.warning("filmspot preview failed", error=str(exc))
        if query:
            try:
                await query.answer("预览发送失败。若在群里，请先打开与 Bot 的私聊。", show_alert=True)
            except Exception:
                pass
        await _reply(update, "预览发送失败，请稍后再试。")
        return
    if not query:
        return
    try:
        if prefer_private and not in_private:
            await query.answer("预览已发到私聊")
        else:
            await query.answer()
    except Exception:
        pass


async def _open_film(update: Update, context, media_type: str, item: dict, *, prefer_private: bool) -> None:
    try:
        parts = await build_parts(media_type, item, context)
    except LookupError as exc:
        if str(exc) == "no_key":
            await _reply(update, "❌ TMDB API 未配置，请在 Web 面板 → 组件 → 影视搜索 中添加 TMDB API Key")
        else:
            await _reply(update, "拉取影片详情失败，请稍后再试。")
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass
        return
    user = update.effective_user
    assert user is not None
    draft = _make_film_draft(user.id, parts)
    await _show_draft(update, context, draft, prefer_private=prefer_private)


async def _begin_search(update: Update, context, media_type: str, query: str) -> None:
    from bot.cache import movie_cache
    from modules.movie import search_tmdb

    msg = update.effective_message
    assert msg is not None
    status = await msg.reply_text("🔍 搜索中…")
    results = await search_tmdb(query, media_type, context)
    try:
        await status.delete()
    except Exception:
        pass
    if results is None:
        await msg.reply_text("❌ TMDB API 未配置，请在 Web 面板 → 组件 → 影视搜索 中添加 TMDB API Key")
        return
    if not results:
        await msg.reply_text(f"❌ 未找到：{query}")
        return
    if len(results) == 1:
        wait = await msg.reply_text("🎬 正在整理频道稿…")
        await _open_film(update, context, media_type, results[0], prefer_private=False)
        try:
            await wait.delete()
        except Exception:
            pass
        return
    short_key = hashlib.md5(f"{media_type}:{query.lower()}".encode()).hexdigest()[:16]
    await movie_cache.set(short_key, results)
    rows: list[list[InlineKeyboardButton]] = []
    lines = [f"搜到 <b>{len(results)}</b> 条，选择要生成频道稿的条目：\n"]
    for index, item in enumerate(results[:5]):
        title = item.get("title") or item.get("name") or "?"
        year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
        label = f"{index + 1}. {title[:18]}"
        if year:
            label += f"（{year}）"
        rows.append([InlineKeyboardButton(
            label[:60],
            callback_data=f"fs_pick:{media_type}:{short_key}:{index}",
        )])
        line = f"{index + 1}. <b>{_esc(title)}</b>"
        if year:
            line += f" ({_esc(year)})"
        lines.append(line)
    await msg.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_filmspot(update: Update, context) -> None:
    if not await _need_private(update):
        return
    user = update.effective_user
    _clear_wait(user.id if user else None)
    args = list(context.args or [])
    if not args:
        await _reply(update, _USAGE)
        return
    head = args[0].lower()
    if head in ("tv", "剧集", "电视剧"):
        media, query = "tv", " ".join(args[1:]).strip()
    elif head in ("movie", "电影"):
        media, query = "movie", " ".join(args[1:]).strip()
    else:
        media, query = "movie", " ".join(args).strip()
    if not query:
        await _reply(update, _USAGE)
        return
    await _begin_search(update, context, media, query)


async def cmd_channel_post(update: Update, context) -> None:
    if not await _need_private(update):
        return
    user = update.effective_user
    _clear_wait(user.id if user else None)
    args = list(context.args or [])
    if len(args) < 2 or args[0].lower() not in ("movie", "tv"):
        await _reply(update, "用法：/channel_post movie|tv <名称>")
        return
    await _begin_search(update, context, args[0].lower(), " ".join(args[1:]).strip())


async def _open_gossip(update: Update, context, text: str, photo_file_id: Optional[str]) -> None:
    user = update.effective_user
    assert user is not None
    body = (text or "").strip()
    if not body and photo_file_id:
        body = "（图片）"
    if not body and not photo_file_id:
        await _reply(update, "没有正文。请发送文字，或文字加一张图片。")
        return
    draft = _make_gossip_draft(user.id, body, photo_file_id)
    await _show_draft(update, context, draft, prefer_private=False)


async def cmd_gossip(update: Update, context) -> None:
    if not await _need_private(update):
        return
    msg = update.effective_message
    user = update.effective_user
    assert msg is not None and user is not None
    _clear_wait(user.id)
    args_text = " ".join(context.args or []).strip()
    if args_text:
        await _open_gossip(update, context, args_text, None)
        return
    reply = msg.reply_to_message
    if reply is not None and (reply.text or reply.caption or reply.photo):
        photo_id = reply.photo[-1].file_id if reply.photo else None
        text = reply.text or reply.caption or ""
        await _open_gossip(update, context, text, photo_id)
        return
    _WAITING[user.id] = _Wait("gossip", "", time.monotonic() + WAIT_TTL)
    await msg.reply_text(
        "请发送花边正文。可以附带一张图片（图片说明会当作正文）。\n"
        "发送 /cancel 放弃。确认前不会发到频道。"
    )


async def cmd_cancel(update: Update, context) -> None:
    user = update.effective_user
    if not user or user.id not in _WAITING:
        await _reply(update, "没有待完成的频道稿。")
        return
    _WAITING.pop(user.id, None)
    await _reply(update, "已取消。")


async def on_make(update: Update, context) -> None:
    query = update.callback_query
    assert query is not None
    if await _blocked(update, context):
        return
    _, media_type, id_str = query.data.split(":")
    await _open_film(
        update, context, media_type, {"id": int(id_str)}, prefer_private=True,
    )


async def on_pick(update: Update, context) -> None:
    from bot.cache import movie_cache

    query = update.callback_query
    assert query is not None
    if await _blocked(update, context):
        return
    _, media_type, cache_key, idx_str = query.data.split(":")
    idx = int(idx_str)
    results = await movie_cache.get(cache_key)
    if not results or idx >= len(results):
        await query.answer("结果已过期，请重新搜索。", show_alert=True)
        return
    await query.answer()
    await _open_film(update, context, media_type, results[idx], prefer_private=False)


async def on_send(update: Update, context) -> None:
    from bot.database import get_bot_config

    query = update.callback_query
    assert query is not None and query.data
    if await _blocked(update, context):
        return
    token = query.data.split(":", 1)[1]
    user = update.effective_user
    draft = _DRAFTS.get(token)
    if not draft or not user or draft.owner_id != user.id or draft.expires <= time.monotonic():
        _DRAFTS.pop(token, None)
        await query.answer("预览已过期，请重新生成。", show_alert=True)
        return
    if draft.inflight:
        await query.answer("正在发送…")
        return
    enabled = await get_bot_config("filmspot_enabled")
    channel_raw = await get_bot_config("filmspot_channel_id")
    reason = publishing_block_reason(enabled, channel_raw)
    if reason:
        await query.answer("还不能发送", show_alert=False)
        if query.message:
            await query.message.reply_text(reason)
        return
    chat_id = parse_channel_id(channel_raw)
    assert chat_id is not None
    draft.inflight = True
    await query.answer("正在发送…")
    try:
        await _publish(context.bot, chat_id, draft)
    except Forbidden:
        draft.inflight = False
        if query.message:
            await query.message.reply_text(
                "发送被拒绝。请把机器人加为该频道的管理员，并核对 filmspot_channel_id。"
            )
        return
    except (BadRequest, TelegramError) as exc:
        draft.inflight = False
        log.warning("filmspot publish failed", error=str(exc))
        if query.message:
            await query.message.reply_text(
                "发送失败。请确认 filmspot_channel_id 正确，且机器人是该频道的管理员。\n"
                f"Telegram：{_esc(str(exc))[:200]}"
            )
        return
    _drop_draft(token)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message:
        await query.message.reply_text("已发送到频道。")
    log.info("filmspot published", kind=draft.kind, channel=str(chat_id))


async def on_cancel_preview(update: Update, context) -> None:
    query = update.callback_query
    assert query is not None and query.data
    if await _blocked(update, context):
        return
    token = query.data.split(":", 1)[1]
    user = update.effective_user
    draft = _DRAFTS.get(token)
    if draft and user and draft.owner_id != user.id:
        await query.answer("这不是你的预览", show_alert=True)
        return
    _drop_draft(token)
    await query.answer("已取消")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if query.message:
        await query.message.reply_text("已取消，未发送。")


async def on_sell(update: Update, context) -> None:
    query = update.callback_query
    assert query is not None and query.data
    if await _blocked(update, context):
        return
    token = query.data.split(":", 1)[1]
    user = update.effective_user
    draft = _DRAFTS.get(token)
    if not draft or not user or draft.owner_id != user.id or not draft.parts:
        await query.answer("预览已过期，请重新生成。", show_alert=True)
        return
    if draft.expires <= time.monotonic():
        _drop_draft(token)
        await query.answer("预览已过期，请重新生成。", show_alert=True)
        return
    _WAITING[user.id] = _Wait("selling", token, time.monotonic() + WAIT_TTL)
    await query.answer()
    if query.message:
        await query.message.reply_text(
            "请发送 1–3 条卖点，一行一条。下一条预览会替换卖点。\n发送 /cancel 放弃修改。"
        )


async def _abandon_wait_on_command(update: Update, context) -> None:
    """Drop a pending paste when the owner sends any command except /cancel."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or user.id not in _WAITING:
        return
    text = (msg.text or msg.caption or "").split()
    command = text[0].split("@")[0].lower() if text else ""
    if command == "/cancel":
        return
    _WAITING.pop(user.id, None)


async def on_draft_input(update: Update, context) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not _is_owner(update, context):
        return
    _purge()
    wait = _WAITING.get(user.id)
    if not wait:
        return
    if wait.kind == "gossip":
        photo_id = msg.photo[-1].file_id if msg.photo else None
        text = msg.caption if msg.photo else (msg.text or "")
        if not (text or "").strip() and not photo_id:
            await msg.reply_text("没有收到正文。请再发一次文字或图片。")
            raise ApplicationHandlerStop
        _WAITING.pop(user.id, None)
        await _open_gossip(update, context, text or "", photo_id)
        raise ApplicationHandlerStop
    if wait.kind == "selling":
        lines = apply_selling_lines(msg.text or msg.caption or "")
        if not lines:
            await msg.reply_text("请按行发送 1–3 条卖点。")
            raise ApplicationHandlerStop
        draft = _DRAFTS.get(wait.token)
        if (
            not draft
            or draft.owner_id != user.id
            or not draft.parts
            or draft.expires <= time.monotonic()
        ):
            _WAITING.pop(user.id, None)
            await msg.reply_text("预览已过期，请重新生成频道稿。")
            raise ApplicationHandlerStop
        draft.parts["selling"] = lines
        draft.caption, draft.followup = render_filmspot(draft.parts)
        draft.expires = time.monotonic() + DRAFT_TTL
        _WAITING.pop(user.id, None)
        try:
            await _deliver(context.bot, msg.chat_id, draft, preview=True)
        except TelegramError as exc:
            log.warning("filmspot selling preview failed", error=str(exc))
            await msg.reply_text("卖点已更新，但预览发送失败。请重新生成频道稿。")
        raise ApplicationHandlerStop


_COMPONENT = Component(
    id="filmspot",
    name="FilmSpot 频道稿",
    icon="bi-megaphone",
    description=(
        "所有者在私聊预览固定格式的电影/剧集稿或花边，确认后发到频道。"
        "频道 ID 只在这里配置，默认为空；未填写时可以预览，点发送会提示来这里设置。"
    ),
    config_keys=[
        ConfigField(
            key="filmspot_channel_id",
            label="频道 ID",
            kind="text",
            help=(
                "发布目标。填 Telegram 频道的数字 ID 或 @username，机器人必须是该频道的管理员。"
                "留空则只能预览，点「发送到频道」会提示来这里填写。"
            ),
            placeholder="@username 或数字 ID",
            default="",
        ),
        ConfigField(
            key="filmspot_enabled",
            label="启用频道发布",
            kind="bool",
            help=(
                "关闭后仍可在私聊预览，但不能发到频道。"
                "还没保存过时视为开启；保存本页时请保持开关为打开。"
            ),
            default="1",
        ),
    ],
    order=32,
)


def _guard(func):
    return require_module("filmspot")(owner_only(func))


def setup(application: Application) -> None:
    register_component(_COMPONENT)
    registry.register_command(
        "filmspot", _guard(cmd_filmspot), "频道稿预览 movie|tv（仅所有者）", admin_only=True,
    )
    registry.register_command(
        "channel_post", _guard(cmd_channel_post), "频道稿 /channel_post movie|tv 名称", admin_only=True,
    )
    registry.register_command(
        "gossip", _guard(cmd_gossip), "花边频道稿（仅所有者）", admin_only=True,
    )
    registry.register_command(
        "channel_text", _guard(cmd_gossip), "花边频道稿，同 /gossip", admin_only=True,
    )
    application.add_handler(CallbackQueryHandler(on_make, pattern=r"^fs_make:(movie|tv):\d+$"))
    application.add_handler(CallbackQueryHandler(on_pick, pattern=r"^fs_pick:(movie|tv):[0-9a-f]+:\d+$"))
    application.add_handler(CallbackQueryHandler(on_send, pattern=r"^fs_send:[0-9a-f]{8}$"))
    application.add_handler(CallbackQueryHandler(on_cancel_preview, pattern=r"^fs_cancel:[0-9a-f]{8}$"))
    application.add_handler(CallbackQueryHandler(on_sell, pattern=r"^fs_sell:[0-9a-f]{8}$"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
            on_draft_input,
        ),
        group=0,
    )
    # Group -1 runs before command handlers (group 0) and does not swallow them.
    application.add_handler(
        MessageHandler(filters.COMMAND, _abandon_wait_on_command),
        group=-1,
    )
    # Not registered in the public command menu; only clears an in-progress draft.
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    log.info("filmspot module loaded")
