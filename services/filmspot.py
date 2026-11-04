"""FilmSpot card shared by /movie, /tv, and inline movie/tv.

Genres and credits come from the TMDB details payload (zh-CN, gaps filled
from en-US). Selling points are polished with the existing DeepSeek key;
a missing key or a failed call keeps tagline, rating, and popularity.
"""
from __future__ import annotations

import asyncio
import html
import re
from typing import Optional

from bot.ai import deepseek_call
from bot.logger import get_logger

log = get_logger(__name__)

CAPTION_LIMIT = 1024
_BULLET_PREFIX = re.compile(r"^[•\-\*·]+|^[\d]+[.、]\s*")
_DIRECTOR_JOBS = {"Director", "导演"}


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
            creators = _people(credits.get("crew"), limit=3, jobs=_DIRECTOR_JOBS)
        if not creators:
            creators = _people(credits.get("crew"), limit=2, jobs={"Executive Producer"})
    else:
        creators = _people(credits.get("crew"), limit=3, jobs=_DIRECTOR_JOBS)
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


def needs_english(detail: dict, media_type: str) -> bool:
    """True when zh-CN is missing a field the card has to show."""
    credits = detail.get("credits") or {}
    cast = credits.get("cast") or (detail.get("aggregate_credits") or {}).get("cast")
    crew = [row for row in (credits.get("crew") or []) if isinstance(row, dict)]
    if not (detail.get("overview") or "").strip():
        return True
    if not (detail.get("tagline") or "").strip():
        return True
    if not detail.get("genres"):
        return True
    if not cast:
        return True
    if media_type == "movie" and not any(row.get("job") in _DIRECTOR_JOBS for row in crew):
        return True
    if media_type == "tv" and not detail.get("created_by"):
        return True
    return False


def apply_language_gaps(primary: Optional[dict], fallback: Optional[dict], media_type: str) -> dict:
    """Copy en-US fields into a zh-CN payload only where zh-CN is empty."""
    merged = dict(primary or {})
    if not fallback:
        return merged
    for key in (
        "overview", "tagline", "title", "name", "original_title", "original_name",
        "release_date", "first_air_date", "poster_path",
    ):
        if not str(merged.get(key) or "").strip() and str(fallback.get(key) or "").strip():
            merged[key] = fallback[key]
    if not merged.get("genres") and fallback.get("genres"):
        merged["genres"] = fallback["genres"]
    if media_type == "tv" and not merged.get("created_by") and fallback.get("created_by"):
        merged["created_by"] = fallback["created_by"]
    for key in (
        "vote_average", "vote_count", "popularity", "runtime", "status",
        "number_of_seasons", "number_of_episodes",
    ):
        if merged.get(key) in (None, "") and fallback.get(key) not in (None, ""):
            merged[key] = fallback[key]
    for key in ("production_countries", "spoken_languages", "networks", "episode_run_time"):
        if not merged.get(key) and fallback.get(key):
            merged[key] = fallback[key]
    credits = dict(merged.get("credits") or {})
    fallback_credits = fallback.get("credits") or {}
    if not credits.get("cast") and fallback_credits.get("cast"):
        credits["cast"] = fallback_credits["cast"]
    crew = list(credits.get("crew") or [])
    if not crew and fallback_credits.get("crew"):
        crew = list(fallback_credits["crew"])
    elif media_type == "movie" and not any(
        isinstance(row, dict) and row.get("job") in _DIRECTOR_JOBS for row in crew
    ):
        directors = [
            row for row in (fallback_credits.get("crew") or [])
            if isinstance(row, dict) and row.get("job") in _DIRECTOR_JOBS
        ]
        if directors:
            crew = directors + crew
    if crew:
        credits["crew"] = crew
    if credits:
        merged["credits"] = credits
    if media_type == "tv" and not (merged.get("aggregate_credits") or {}).get("cast"):
        if (fallback.get("aggregate_credits") or {}).get("cast"):
            merged["aggregate_credits"] = fallback["aggregate_credits"]
    return merged


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


def trim_to_limit(text: str, limit: int) -> str:
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
        head = trim_to_limit(_body(parts, "…", False), CAPTION_LIMIT)
    elif utf16_len(head) > CAPTION_LIMIT:
        head = trim_to_limit(head, CAPTION_LIMIT)
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
    return trim_to_limit(text, limit)


async def polish_selling(parts: dict) -> Optional[str]:
    """Ask DeepSeek for 1–3 bullets. None when the key is missing or the call fails."""
    overview = (parts.get("overview") or "").strip()
    tagline = (parts.get("tagline") or "").strip()
    if overview == "暂无梗概。":
        overview = ""
    if not overview and not tagline:
        return None
    try:
        async with asyncio.timeout(5):
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


async def _format_movie(item: dict, media_type: str = "movie", context=None) -> str:
    """FilmSpot text for one TMDB object. No network unless a caller passes context."""
    parts = parts_from_detail(item or {}, media_type)
    if context is not None:
        parts["selling"] = choose_selling_points(await polish_selling(parts), parts["selling"])
    caption, followup = render_filmspot(parts)
    return card_text(caption, followup)
