"""
Movie & TV query module — powered by TMDB API.

Accessible via Telegram inline mode: @bot movie <title>  /  @bot tv <title>
Requires TMDB_API_KEY (env or Web Admin UI → Settings).
Results cached for 1 hour per query string.

Both /movie, /tv, and inline movie/tv use one FilmSpot-style card
(片名, 原名, 类型, 主创, 档期, 梗概, 卖点, 标签). Genres and credits come
from the details endpoint. When deepseek_api_key is set, DeepSeek rewrites
the 卖点; otherwise the card uses the tagline, rating, and popularity.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Optional

import aiohttp

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import movie_cache
from bot.components import Component, ConfigField, register_component, require_module
from bot.database import get_bot_config
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

_TMDB_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_TMDB_URL = "https://www.themoviedb.org"
CAPTION_LIMIT = 1024
_BULLET_PREFIX = re.compile(r"^[•\-\*·]+|^[\d]+[.、]\s*")


async def _get_api_key(context) -> Optional[str]:
    key = await get_bot_config("tmdb_api_key")
    if key:
        return key
    cfg = context.bot_data.get("config") if context else None
    return getattr(cfg, "tmdb_api_key", None) if cfg else None


async def _tmdb_get(path: str, api_key: str, **params) -> Optional[dict]:
    url = f"{_TMDB_BASE}{path}"
    params["api_key"] = api_key
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        log.warning("TMDB request failed", error=str(e))
        return None


def utf16_len(text: str) -> int:
    """Telegram counts caption length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _hashtag(label: str) -> str:
    return re.sub(r"[^\w]+", "", label or "", flags=re.UNICODE).strip("_")


def derive_selling_points(detail: dict) -> list[str]:
    """Up to three bullets from tagline, score, and popularity. No network."""
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
    """Take up to three non-empty lines, stripping a leading bullet or number."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = _BULLET_PREFIX.sub("", raw.strip()).strip()
        if not line:
            continue
        lines.append(line)
        if len(lines) == 3:
            break
    return lines


def choose_selling_points(polished: Optional[str], derived: list[str]) -> list[str]:
    """Prefer DeepSeek lines. Empty or failed output keeps the derived bullets."""
    lines = apply_selling_lines(polished or "")
    return lines or list(derived)


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


def _people(rows: Optional[list], *, limit: int, jobs: Optional[set[str]] = None) -> list[str]:
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
    """Normalize a TMDB movie/TV object into FilmSpot fields."""
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
            creators = _people(credits.get("crew"), limit=2, jobs={"Executive Producer"})
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
        "tagline": (detail.get("tagline") or "").strip(),
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
        rows = ["（待补充卖点）"]
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
    """Return ``(caption, followup)``. Caption stays within Telegram's photo limit.

    Follow-up is empty when the whole template fits. When the header plus
    selling points cannot sit beside the synopsis, selling points and tags
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


def card_text(caption: str, followup: str, limit: int = 4000) -> str:
    """Full card for a text message or inline result."""
    text = caption if not followup else f"{caption}\n\n{followup}"
    if utf16_len(text) <= limit:
        return text
    return _trim_to_limit(text, limit)


async def fetch_detail(media_type: str, tmdb_id: int, context) -> Optional[dict]:
    """Details + credits. Prefer zh-CN; fill gaps from en-US."""
    api_key = await _get_api_key(context)
    if not api_key:
        return None
    cache_key = f"detail:{media_type}:{tmdb_id}"
    cached = await movie_cache.get(cache_key)
    if cached:
        return cached
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
        c.get("job") in ("Director", "导演") for c in (credits.get("crew") or [])
    ):
        needs_en = True
    if media_type == "tv" and not zh.get("created_by"):
        needs_en = True
    if needs_en:
        en = await _tmdb_get(
            f"/{media_type}/{tmdb_id}",
            api_key,
            language="en-US",
            append_to_response=extra,
        )
        if en:
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
    await movie_cache.set(cache_key, zh)
    return zh


async def _polish_selling(parts: dict) -> Optional[str]:
    """Ask DeepSeek for 1–3 bullets. None when the key is missing or the call fails."""
    overview = (parts.get("overview") or "").strip()
    tagline = (parts.get("tagline") or "").strip()
    if overview == "暂无梗概。":
        overview = ""
    if not overview and not tagline:
        return None
    try:
        from bot.ai import deepseek_call
        return await deepseek_call(
            "movie",
            "selling_points",
            [
                {
                    "role": "system",
                    "content": (
                        "你是影视编辑。只输出 1 到 3 行中文卖点，每行一条，"
                        "不要编号、引号或解释。每条不超过 36 个字。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"片名：{parts.get('title') or ''}\n"
                        f"标语：{tagline}\n"
                        f"梗概：{overview[:800]}"
                    ),
                },
            ],
            max_tokens=180,
            temperature=0.4,
        )
    except Exception as exc:
        log.warning("selling-point polish failed", error=str(exc))
        return None


async def compose_card(item: dict, media_type: str, context=None) -> tuple[str, str, str]:
    """Build caption, optional follow-up, and poster path. Never raises for a missing AI key."""
    detail = None
    raw_id = (item or {}).get("id")
    if context is not None and raw_id is not None:
        try:
            detail = await fetch_detail(media_type, int(raw_id), context)
        except (TypeError, ValueError):
            detail = None
        except Exception as exc:
            log.warning("TMDB detail failed", error=str(exc))
            detail = None
    parts = parts_from_detail(merge_media(item, detail), media_type)
    if context is not None:
        polished = await _polish_selling(parts)
        parts["selling"] = choose_selling_points(polished, parts["selling"])
    caption, followup = render_filmspot(parts)
    return caption, followup, parts.get("poster_path") or ""


async def _format_movie(item: dict, media_type: str = "movie", context=None) -> str:
    """FilmSpot text used by /movie, /tv, and inline movie/tv."""
    caption, followup, _poster = await compose_card(item, media_type, context)
    return card_text(caption, followup)


async def search_tmdb(query: str, media_type: str, context) -> Optional[list]:
    """Search TMDB and return a list of up to 5 result dicts, or None on error."""
    api_key = await _get_api_key(context)
    if not api_key:
        return None
    cache_key = f"{media_type}:{query.lower()}"
    cached = await movie_cache.get(cache_key)
    if cached:
        return cached
    endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
    data = await _tmdb_get(endpoint, api_key, query=query, language="zh-CN,en-US")
    if not data or not data.get("results"):
        return []
    results = data["results"][:5]
    await movie_cache.set(cache_key, results)
    return results


def _movie_select_keyboard(items: list, cache_key: str, media_type: str) -> InlineKeyboardMarkup:
    rows: list = []
    row: list = []
    for i, item in enumerate(items[:5]):
        title = (item.get("title") or item.get("name", "?"))[:18]
        year = (item.get("release_date") or item.get("first_air_date", ""))[:4]
        label = f"{i+1}. {title}（{year}）" if year else f"{i+1}. {title}"
        row.append(InlineKeyboardButton(label, callback_data=f"movie_sel:{media_type}:{cache_key}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _emit_card(item: dict, media_type: str, context, *, reply=None, chat_id=None) -> None:
    """Send the poster with a FilmSpot caption. Overflow text follows the photo."""
    caption, followup, poster_path = await compose_card(item, media_type, context)
    photo = f"{_IMG_BASE}{poster_path}" if poster_path else None

    async def _text(body: str) -> None:
        if reply is not None:
            await reply.reply_text(body, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        elif chat_id is not None:
            await context.bot.send_message(
                chat_id, body, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )

    if photo:
        try:
            if reply is not None:
                await reply.reply_photo(photo, caption=caption, parse_mode=ParseMode.HTML)
            elif chat_id is not None:
                await context.bot.send_photo(
                    chat_id, photo, caption=caption, parse_mode=ParseMode.HTML,
                )
            else:
                raise RuntimeError("no destination")
            if followup:
                body = followup if utf16_len(followup) <= 4000 else _trim_to_limit(followup, 4000)
                await _text(body)
            return
        except Exception as exc:
            log.warning("movie photo send failed", error=str(exc))
    await _text(card_text(caption, followup))


async def on_movie_select(update: Update, context) -> None:
    q = update.callback_query
    assert q
    await q.answer()

    _, media_type, cache_key, idx_str = q.data.split(":", 3)
    idx = int(idx_str)

    results = await movie_cache.get(cache_key)
    if not results or idx >= len(results):
        await q.edit_message_text("结果已过期，请重新搜索。")
        return

    await q.edit_message_text("⏳ 加载中…", reply_markup=None)

    item = results[idx]
    chat_id = q.message.chat_id if q.message else None

    try:
        await q.message.delete()
    except Exception:
        pass

    if chat_id:
        await _emit_card(item, media_type, context, chat_id=chat_id)


async def _cmd_search(update: Update, context, media_type: str) -> None:
    msg = update.effective_message
    if not context.args:
        label = "电影" if media_type == "movie" else "剧集"
        cmd = "movie" if media_type == "movie" else "tv"
        await msg.reply_text(f"用法：/{cmd} <{label}名>")
        return
    query = " ".join(context.args)
    status = await msg.reply_text("🔍 搜索中…")
    results = await search_tmdb(query, media_type, context)
    await status.delete()

    if results is None:
        await msg.reply_text("❌ TMDB API 未配置，请在 Web 面板 → 设置 中添加 TMDB API Key")
        return
    if not results:
        await msg.reply_text(f"❌ 未找到：{query}")
        return

    short_key = hashlib.md5(f"{media_type}:{query.lower()}".encode()).hexdigest()[:16]
    await movie_cache.set(short_key, results)

    if len(results) == 1:
        await _emit_card(results[0], media_type, context, reply=msg)
    else:
        emoji = "🎬" if media_type == "movie" else "📺"
        lines = [f"{emoji} 搜到 <b>{len(results)}</b> 个结果，请选择：\n"]
        for i, item in enumerate(results[:5]):
            title = item.get("title") or item.get("name", "?")
            year = (item.get("release_date") or item.get("first_air_date", ""))[:4]
            rating = item.get("vote_average", 0)
            line = f"{i+1}. <b>{title}</b>"
            if year:    line += f" ({year})"
            if rating:  line += f" · ⭐{rating:.1f}"
            lines.append(line)
        keyboard = _movie_select_keyboard(results, short_key, media_type)
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                             reply_markup=keyboard)


async def cmd_movie(update: Update, context) -> None:
    await _cmd_search(update, context, "movie")


async def cmd_tv(update: Update, context) -> None:
    await _cmd_search(update, context, "tv")


_COMPONENT = Component(
    id="movie",
    name="影视搜索",
    icon="bi-film",
    description=(
        "/movie /tv 及 inline 影视搜索，数据来自 TMDB。"
        "结果为固定卡片：片名、原名、类型、主创、档期、梗概、卖点、标签。"
        "卖点在配置了全局 DeepSeek Key 时润色，否则用标语和评分。"
    ),
    config_keys=[
        ConfigField(
            key="tmdb_api_key",
            label="TMDB API Key",
            kind="secret",
            help='注册 themoviedb.org → 账号设置 → API → 申请 Developer Key。格式：32 位十六进制。',
            placeholder="32位十六进制",
            test_service="tmdb",
            link="https://www.themoviedb.org/settings/api",
            link_label="themoviedb.org",
        ),
    ],
    order=30,
)


def setup(application: Application) -> None:
    register_component(_COMPONENT)
    registry.register_command("movie", cmd_movie, "搜索电影 <片名>")
    registry.register_command("tv", cmd_tv, "搜索剧集 <剧名>")
    application.add_handler(CallbackQueryHandler(on_movie_select, pattern=r"^movie_sel:"))
    application.add_handler(CommandHandler("movie", require_module("movie")(cmd_movie)))
    application.add_handler(CommandHandler("tv", require_module("movie")(cmd_tv)))
    log.info("movie module loaded")
