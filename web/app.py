"""
Ponygram web admin UI.

Start via main.py when WEB_ENABLED=true.  Provides:
  /           — dashboard (stats + service health panel)
  /groups     — list all groups; search/activate new groups
  /groups/<id>— group settings form
  /blacklist  — view, add, remove global blacklist entries
  /rss        — manage RSS subscriptions across all chats
  /settings   — bot-level API key configuration
  /health     — JSON service health (used by dashboard JS)
  /health/check/{service} — live-test a specific service
  /health/check/media     — live-test media URL parsing via ParseHub
  /psn-library — PS Plus game library management (CRUD + bulk import)
  /login      — password form (WEB_SECRET from .env)
  /logout     — clear session
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import os
import secrets
from datetime import date as _date
from pathlib import Path
from typing import Any, List, Optional

import aiohttp
from fastapi import Cookie, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bot.database import (
    Blacklist,
    GroupSettings,
    PsnColumnsConfig,
    PsnLibraryGame,
    RssFeed,
    User,
    UserWarn,
    add_rss_feed,
    add_to_blacklist,
    bulk_import_psn_games,
    count_psn_library_games,
    delete_bot_config,
    delete_psn_column,
    delete_psn_game,
    get_ai_usage_log,
    get_ai_usage_stats,
    get_all_bot_configs,
    get_all_rss_feeds,
    get_bot_config,
    get_group_settings,
    get_psn_columns,
    get_psn_game_by_id,
    get_psn_library_games,
    get_session,
    patch_psn_game_cell,
    remove_from_blacklist,
    remove_rss_feed,
    set_bot_config,
    set_feed_paused,
    set_group_field,
    upsert_psn_column,
    upsert_psn_game,
)
from bot.utils import normalize_cookie
from sqlalchemy import func, select

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_secret: str = ""
_bot: Any = None  # telegram.Bot instance, injected at startup


# ---------------------------------------------------------------------------
# Session helpers (HMAC-signed cookie)
# ---------------------------------------------------------------------------

def _make_token() -> str:
    nonce = secrets.token_hex(16)
    sig = hashlib.sha256(f"{_secret}:{nonce}".encode()).hexdigest()
    return f"{nonce}.{sig}"


def _verify_token(token: str) -> bool:
    try:
        nonce, sig = token.split(".", 1)
        expected = hashlib.sha256(f"{_secret}:{nonce}".encode()).hexdigest()
        return secrets.compare_digest(sig, expected)
    except Exception:
        return False


def _authed(session: Optional[str]) -> bool:
    return bool(session and _verify_token(session))


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _parse_chat_ref(q: str) -> Optional[str]:
    """Return a chat identifier (int string or @username) from user input."""
    q = q.strip()
    if not q:
        return None
    if q.lstrip("-").isdigit():
        return q
    if "t.me/" in q:
        username = q.split("t.me/")[-1].split("/")[0].split("?")[0]
        return f"@{username}" if not username.startswith("@") else username
    if q.startswith("@"):
        return q
    return None


# ---------------------------------------------------------------------------
# PSN Library helpers
# ---------------------------------------------------------------------------

async def _psn_lib_save_form(game_id: Optional[int], form) -> None:
    def _d(key: str):
        val = (form.get(key) or "").strip()
        return _date.fromisoformat(val) if val else None

    def _i(key: str) -> Optional[int]:
        val = (form.get(key) or "").strip()
        return int(val) if val else None

    platforms_parts = []
    if form.get("platform_ps4"):
        platforms_parts.append("PS4")
    if form.get("platform_ps5"):
        platforms_parts.append("PS5")

    await upsert_psn_game(
        title_en=(form.get("title_en") or "").strip(),
        entry_date=_d("entry_date") or _date.today(),
        tier=int(form.get("tier") or 2),
        game_id=game_id,
        title_zh=(form.get("title_zh") or "").strip() or None,
        concept_id=_i("concept_id"),
        product_id=(form.get("product_id") or "").strip() or None,
        store_url=(form.get("store_url") or "").strip() or None,
        cover_url=(form.get("cover_url") or "").strip() or None,
        release_date=_d("release_date"),
        exit_date=_d("exit_date"),
        status=(form.get("status") or "active").strip(),
        platforms=",".join(platforms_parts) if platforms_parts else None,
        genre=(form.get("genre") or "").strip() or None,
        age_rating=(form.get("age_rating") or "").strip() or None,
        streaming=bool(form.get("streaming")),
        region=(form.get("region") or "HK").strip() or "HK",
        note=(form.get("note") or "").strip() or None,
    )


def _parse_psn_csv(csv_text: str) -> List[dict]:
    """Parse CSV text into list of dicts; normalise pipe-separated platforms."""
    rows = []
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    for row in reader:
        cleaned = {k.strip(): v.strip() for k, v in row.items()}
        # Accept both | and , as platform separator
        if "platforms" in cleaned:
            cleaned["platforms"] = cleaned["platforms"].replace("|", ",")
        rows.append(cleaned)
    return rows


import json as _json
import re as _re
from datetime import datetime as _dt


_SKIP_EN = _re.compile(
    r'^\s*[-–]|PS[45]|PlayStation|^&\s*PS[45]',
    _re.IGNORECASE
)

_LANG_PAREN = _re.compile(
    r'[\(（](?:[简繁體体]+[中文日英韩泰韓越]+|.+?[文版本]|中.{0,4}英.{0,4}韩.{0,4}'
    r'|Chinese|Korean|Japanese|English|version)[^)）]*[\)）]',
    _re.IGNORECASE,
)
_PLAT_PAREN = _re.compile(r'[\(（]\s*PS[45].{0,30}?[\)）]', _re.IGNORECASE)
_TAIL_TAGS  = _re.compile(
    r'\s*(?:PS[45][?？]?\s*[&和]?\s*PS[45][?？]?|主機版|PlayStation\s*Plus)\s*$',
    _re.IGNORECASE,
)


def _clean_zh_name(s: str) -> str:
    """Strip language/platform noise from a PS Store Chinese title."""
    s = _LANG_PAREN.sub('', s)
    s = _PLAT_PAREN.sub('', s)
    s = _re.sub(r'\s*[\(（]\s*[\)）]', '', s)  # empty brackets
    s = _TAIL_TAGS.sub('', s)
    return s.strip(' \t　《》')


def _extract_en_from_name(name: str) -> tuple[str, str]:
    """
    Extract (title_en, title_zh) from PS Store name field.
    Handles: 《CJK》English, 《English》, pure English, pure CJK.
    Returns (en, zh) — either may be empty string.
    """
    name = name.strip()

    m = _re.search(r'《([^》]+)》', name)
    if m:
        inner = m.group(1).strip()
        after_raw = name[m.end():].strip()
        # Text before first parenthesis after 》
        after_pre = _re.split(r'[\(（]', after_raw)[0].strip().rstrip('–- \t')

        if _re.search(r'[一-鿿぀-ヿ가-힯]', inner):
            # CJK title inside 《》
            zh = _clean_zh_name(inner)
            # Look for English title text after 》
            if (after_pre and len(after_pre) >= 3
                    and not _re.search(r'[一-鿿぀-ヿ가-힯]', after_pre)
                    and _re.search(r'[A-Za-z]', after_pre)
                    and not _SKIP_EN.search(after_pre)):
                return after_pre, zh
            return "", zh
        else:
            # English title inside 《》
            return inner, ""

    if not _re.search(r'[一-鿿぀-ヿ가-힯]', name):
        # Pure English — strip platform/edition suffixes
        en = _re.split(r'[\(（]', name)[0].strip()
        en = _re.sub(r'\s*[-–]\s*(PS[45]|PlayStation[45]?|中文版|繁體|簡體|英文|日文).*$',
                     '', en, flags=_re.IGNORECASE).strip()
        return en, ""

    return "", _clean_zh_name(name)


def _parse_ps_store_csv_bytes(data: bytes) -> List[dict]:
    """
    Parse PS Store export CSV (GBK-encoded) into normalized psn_library_games dicts.
    PS Store columns: name, service_tier, platforms, product_id, product_url,
                      image_url, is_active, first_seen_at
    """
    # Detect encoding
    for enc in ("gbk", "utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    tier_map = {"ESSENTIAL": 1, "EXTRA": 2, "PREMIUM": 3}
    rows = []
    needs_translation: List[tuple[int, str]] = []  # (index, zh_name)

    for row in reader:
        raw_name = (row.get("name") or "").strip()
        if not raw_name:
            continue

        title_en, title_zh = _extract_en_from_name(raw_name)

        tier_raw = (row.get("service_tier") or "").strip().upper()
        tier = tier_map.get(tier_raw, 2)

        # Platforms: JSON array like ["PS4","PS5"] or comma-sep
        plat_raw = (row.get("platforms") or "").strip()
        try:
            plat_list = _json.loads(plat_raw)
            platforms = ",".join(plat_list) if isinstance(plat_list, list) else plat_raw
        except Exception:
            platforms = plat_raw.replace("|", ",")

        # Dates
        first_seen = (row.get("first_seen_at") or "").strip()
        entry_date = None
        for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                entry_date = _dt.strptime(first_seen, fmt).date().isoformat()
                break
            except ValueError:
                continue

        status = "active" if (row.get("is_active") or "").strip() == "1" else "removed"

        normalized = {
            "title_en": title_en,
            "title_zh": title_zh,
            "tier": str(tier),
            "product_id": (row.get("product_id") or "").strip() or "",
            "store_url": (row.get("product_url") or "").strip() or "",
            "cover_url": (row.get("image_url") or "").strip() or "",
            "platforms": platforms,
            "entry_date": entry_date or "",
            "status": status,
            "region": "HK",
        }
        idx = len(rows)
        rows.append(normalized)
        if not title_en and title_zh:
            needs_translation.append((idx, title_zh))

    return rows, needs_translation


# ---------------------------------------------------------------------------
# Media platform registry (shared by /media page and /health/check/media)
# ---------------------------------------------------------------------------

_MEDIA_PLATFORMS = [
    {
        "id": "bilibili",
        "name": "哔哩哔哩",
        "bi_icon": "bi-play-btn-fill",
        "icon_color": "#00aeec",
        "domains": ["bilibili.com", "b23.tv", "bili2233.cn"],
        "cookie_key": "cookie_bilibili",
        "cookie_required": True,
        "example_url": "https://www.bilibili.com/video/BV14fQ4BEEWd",
        "parse_mode": "parsehub",
        "notes": "需要 Cookie，parsehub 负责传递",
    },
    {
        "id": "youtube",
        "name": "YouTube",
        "bi_icon": "bi-youtube",
        "icon_color": "#ff0000",
        "domains": ["youtube.com", "youtu.be"],
        "cookie_key": "cookie_youtube",
        "cookie_required": False,
        "example_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "parse_mode": "parsehub",
        "notes": "公开视频无需 Cookie",
    },
    {
        "id": "instagram",
        "name": "Instagram",
        "bi_icon": "bi-instagram",
        "icon_color": "#e1306c",
        "domains": ["instagram.com"],
        "cookie_key": "cookie_instagram",
        "cookie_required": True,
        "example_url": "https://www.instagram.com/p/C8SmBpgS52J/",
        "parse_mode": "parsehub",
        "notes": "需要登录 Cookie",
    },
    {
        "id": "tiktok",
        "name": "TikTok",
        "bi_icon": "bi-tiktok",
        "icon_color": "#69c9d0",
        "domains": ["tiktok.com", "vm.tiktok.com"],
        "cookie_key": "cookie_tiktok",
        "cookie_required": False,
        "example_url": "https://www.tiktok.com/@billieeilish/video/7305458431616867630",
        "parse_mode": "parsehub",
        "notes": "部分私密视频需要 Cookie",
    },
    {
        "id": "douyin",
        "name": "抖音",
        "bi_icon": "bi-music-note-beamed",
        "icon_color": "#ff2c55",
        "domains": ["douyin.com"],
        "cookie_key": "cookie_douyin",
        "cookie_required": False,
        "example_url": "https://www.douyin.com/video/7304016981352050994",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "twitter",
        "name": "Twitter / X",
        "bi_icon": "bi-twitter-x",
        "icon_color": "#e7e9ea",
        "domains": ["twitter.com", "x.com", "t.co"],
        "cookie_key": "cookie_twitter",
        "cookie_required": True,
        "example_url": "https://twitter.com/jack/status/20",
        "parse_mode": "parsehub",
        "notes": "需要登录 Cookie（含 auth_token）",
    },
    {
        "id": "weibo",
        "name": "微博",
        "bi_icon": "bi-rss-fill",
        "icon_color": "#e6162d",
        "domains": ["weibo.com"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://weibo.com/tv/show/1034:5012525753786399",
        "parse_mode": "parsehub",
        "notes": "无需 Cookie",
    },
    {
        "id": "xhs",
        "name": "小红书",
        "bi_icon": "bi-journal-richtext",
        "icon_color": "#ff2442",
        "domains": ["xiaohongshu.com", "xhslink.com"],
        "cookie_key": "cookie_xiaohongshu",
        "cookie_required": True,
        "example_url": "https://www.xiaohongshu.com/explore/63b00e630000000025015e99",
        "parse_mode": "parsehub",
        "notes": "需要登录 Cookie",
    },
    {
        "id": "kuaishou",
        "name": "快手",
        "bi_icon": "bi-camera-video-fill",
        "icon_color": "#ff5000",
        "domains": ["kuaishou.com", "ks.app"],
        "cookie_key": "cookie_kuaishou",
        "cookie_required": False,
        "example_url": "https://www.kuaishou.com/short-video/3xhi3i3amuvtgek",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "facebook",
        "name": "Facebook",
        "bi_icon": "bi-facebook",
        "icon_color": "#1877f2",
        "domains": ["facebook.com", "fb.watch"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://www.facebook.com/watch?v=1234567890",
        "parse_mode": "parsehub",
        "notes": "无需 Cookie",
    },
    {
        "id": "tieba",
        "name": "贴吧",
        "bi_icon": "bi-chat-dots-fill",
        "icon_color": "#2468cc",
        "domains": ["tieba.baidu.com"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://tieba.baidu.com/p/8765432109",
        "parse_mode": "parsehub",
        "notes": "无需 Cookie",
    },
    {
        "id": "threads",
        "name": "Threads",
        "bi_icon": "bi-at",
        "icon_color": "#a0a0a0",
        "domains": ["threads.net"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://www.threads.net/@zuck/post/CuFRyFxLPCF",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "weixin",
        "name": "微信公众号",
        "bi_icon": "bi-wechat",
        "icon_color": "#07c160",
        "domains": ["mp.weixin.qq.com", "weixin.qq.com"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://mp.weixin.qq.com/s/abcdefg12345",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "xiaoheihe",
        "name": "小黑盒",
        "bi_icon": "bi-box-fill",
        "icon_color": "#888888",
        "domains": ["xiaoheihe.cn"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://xiaoheihe.cn/community/game/xxx",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "coolapk",
        "name": "酷安",
        "bi_icon": "bi-android2",
        "icon_color": "#1b8e6e",
        "domains": ["coolapk.com"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://www.coolapk.com/feed/12345678",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "pipix",
        "name": "皮皮虾",
        "bi_icon": "bi-emoji-laughing-fill",
        "icon_color": "#ff5c00",
        "domains": ["pipix.com"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://h5.pipix.com/s/abcdef",
        "parse_mode": "parsehub",
        "notes": "",
    },
    {
        "id": "zuiyou",
        "name": "最右",
        "bi_icon": "bi-emoji-smile-fill",
        "icon_color": "#ff6600",
        "domains": ["izuiyou.com"],
        "cookie_key": None,
        "cookie_required": False,
        "example_url": "https://share.izuiyou.com/s/abcdef",
        "parse_mode": "parsehub",
        "notes": "",
    },
]

# Derived domain→cookie_key map (used by /health/check/media)
_MEDIA_COOKIE_MAP_GLOBAL: dict[str, str] = {}
for _p in _MEDIA_PLATFORMS:
    if _p["cookie_key"]:
        for _d in _p["domains"]:
            _MEDIA_COOKIE_MAP_GLOBAL[_d] = _p["cookie_key"]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_web_app(secret: str, bot=None) -> FastAPI:
    global _secret, _bot
    _secret = secret
    _bot = bot

    app = FastAPI(title="Ponygram Admin", docs_url=None, redoc_url=None)

    _MEDIA_COOKIE_MAP = _MEDIA_COOKIE_MAP_GLOBAL

    # ------------------------------------------------------------------ #
    # Auth                                                                 #
    # ------------------------------------------------------------------ #

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str = ""):
        return templates.TemplateResponse(request, "login.html", {"error": error})

    @app.post("/login")
    async def login_post(password: str = Form(...)):
        if not _secret or password != _secret:
            return RedirectResponse(url="/login?error=Invalid+password", status_code=303)
        token = _make_token()
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie("session")
        return resp

    # ------------------------------------------------------------------ #
    # Dashboard                                                            #
    # ------------------------------------------------------------------ #

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()

        async with get_session() as s:
            user_count = (await s.execute(select(func.count()).select_from(User))).scalar_one()
            group_count = (await s.execute(select(func.count()).select_from(GroupSettings))).scalar_one()
            active_count = (await s.execute(
                select(func.count()).select_from(GroupSettings).where(GroupSettings.is_active == True)  # noqa: E712
            )).scalar_one()
            bl_count = (await s.execute(select(func.count()).select_from(Blacklist))).scalar_one()
            warn_count = (await s.execute(select(func.count()).select_from(UserWarn))).scalar_one()
            rss_count = (await s.execute(select(func.count()).select_from(RssFeed))).scalar_one()

        return templates.TemplateResponse(request, "dashboard.html", {
            "active": "dashboard",
            "user_count": user_count,
            "group_count": group_count,
            "active_count": active_count,
            "bl_count": bl_count,
            "warn_count": warn_count,
            "rss_count": rss_count,
        })

    # ------------------------------------------------------------------ #
    # Health checks                                                        #
    # ------------------------------------------------------------------ #

    async def _passive_health() -> dict:
        """Return configured/missing status for all services (no live calls)."""
        configs = await get_all_bot_configs()
        yt_dlp_ok = False
        try:
            import yt_dlp  # noqa: F401
            yt_dlp_ok = True
        except ImportError:
            pass

        services = {
            "telegram": {
                "label": "Telegram Bot",
                "icon": "bi-telegram",
                "status": "ok" if _bot else "missing",
                "detail": f"@{_bot.username}" if _bot and hasattr(_bot, "username") and _bot.username else ("Connected" if _bot else "Bot not injected"),
                "testable": True,
                "setup_url": "https://t.me/BotFather",
                "setup_hint": "Get a token from @BotFather",
            },
            "claude": {
                "label": "Claude AI",
                "icon": "bi-robot",
                "status": "ok" if configs.get("claude_api_key") else "missing",
                "detail": "Key configured" if configs.get("claude_api_key") else "Not configured",
                "testable": bool(configs.get("claude_api_key")),
                "setup_url": "https://console.anthropic.com",
                "setup_hint": "Sign up at console.anthropic.com → API Keys → Create Key",
                "format_hint": "sk-ant-api03-…",
            },
            "tmdb": {
                "label": "TMDB (movie/TV)",
                "icon": "bi-film",
                "status": "ok" if configs.get("tmdb_api_key") else "missing",
                "detail": "Key configured" if configs.get("tmdb_api_key") else "Not configured",
                "testable": bool(configs.get("tmdb_api_key")),
                "setup_url": "https://www.themoviedb.org/settings/api",
                "setup_hint": "themoviedb.org → Settings → API → Request Developer key",
                "format_hint": "32 hex chars, e.g. a1b2c3d4e5f6…",
            },
            "lastfm": {
                "label": "Last.fm (music)",
                "icon": "bi-music-note-beamed",
                "status": "ok" if configs.get("lastfm_api_key") else "missing",
                "detail": "Key configured" if configs.get("lastfm_api_key") else "Not configured",
                "testable": bool(configs.get("lastfm_api_key")),
                "setup_url": "https://www.last.fm/api/account/create",
                "setup_hint": "last.fm/api/account/create → copy API key",
                "format_hint": "32 hex chars",
            },
            "rawg": {
                "label": "RAWG.io (games)",
                "icon": "bi-controller",
                "status": "ok" if configs.get("rawg_api_key") else "missing",
                "detail": "Key configured" if configs.get("rawg_api_key") else "Not configured",
                "testable": bool(configs.get("rawg_api_key")),
                "setup_url": "https://rawg.io/apidocs",
                "setup_hint": "rawg.io/apidocs → Get API Key (free, no card)",
                "format_hint": "40 hex chars",
            },
            "ytdlp": {
                "label": "yt-dlp (media)",
                "icon": "bi-download",
                "status": "ok" if yt_dlp_ok else "missing",
                "detail": "Available" if yt_dlp_ok else "Not installed — run: pip install yt-dlp",
                "testable": yt_dlp_ok,
                "setup_url": "https://github.com/yt-dlp/yt-dlp",
                "setup_hint": "pip install yt-dlp",
                "format_hint": None,
            },
        }
        return services

    @app.get("/health")
    async def health_status(session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        services = await _passive_health()
        return JSONResponse({k: {
            "label": v["label"],
            "status": v["status"],
            "detail": v["detail"],
            "testable": v["testable"],
        } for k, v in services.items()})

    @app.post("/health/check/media")
    async def health_check_media(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        url = (body.get("url") or "").strip()
        if not url:
            return JSONResponse({"status": "error", "detail": "url is required"}, status_code=400)

        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY")

        cookie = None
        url_lower = url.lower()
        for domain, key in _MEDIA_COOKIE_MAP.items():
            if domain in url_lower:
                cookie = await get_bot_config(key)
                break

        try:
            from parsehub import ParseHub
            from parsehub.errors import UnknownPlatform, ParseError

            ph = ParseHub()
            result = await asyncio.wait_for(ph.parse(url, proxy=proxy, cookie=cookie), timeout=30)
            platform = getattr(getattr(result, "platform", None), "display_name", None) or type(result).__name__
            media_list = result.media if isinstance(result.media, list) else ([result.media] if result.media else [])
            media_info = [{"type": type(m).__name__, "url": str(getattr(m, "url", "") or "")[:120]} for m in media_list]
            return JSONResponse({
                "status": "ok",
                "platform": platform,
                "title": str(getattr(result, "title", "") or "")[:100],
                "result_type": type(result).__name__,
                "media_count": len(media_info),
                "media": media_info[:3],
                "proxy_used": proxy or None,
                "cookie_used": bool(cookie),
            })
        except Exception as e:
            import traceback as _tb
            err_type = type(e).__name__
            return JSONResponse({
                "status": "error",
                "error_type": err_type,
                "detail": str(e)[:300],
                "traceback": _tb.format_exc()[-800:],
                "proxy_used": proxy or None,
                "cookie_used": bool(cookie),
            })

    @app.post("/health/check/{service}")
    async def health_check_service(service: str, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        configs = await get_all_bot_configs()

        async def _http_get(url: str, **kwargs) -> tuple[int, dict]:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=8), **kwargs) as r:
                        try:
                            body = await r.json()
                        except Exception:
                            body = {}
                        return r.status, body
            except Exception as e:
                return 0, {"error": str(e)}

        if service == "telegram":
            if not _bot:
                return JSONResponse({"status": "error", "detail": "Bot not available"})
            try:
                me = await asyncio.wait_for(_bot.get_me(), timeout=5)
                return JSONResponse({"status": "ok", "detail": f"@{me.username} — {me.first_name}"})
            except Exception as e:
                return JSONResponse({"status": "error", "detail": str(e)})

        elif service == "claude":
            key = configs.get("claude_api_key")
            if not key:
                return JSONResponse({"status": "missing", "detail": "API key not configured"})
            try:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=key)
                msg = await asyncio.wait_for(
                    client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=1,
                        messages=[{"role": "user", "content": "hi"}],
                    ),
                    timeout=10,
                )
                return JSONResponse({"status": "ok", "detail": f"Model {msg.model} — key valid"})
            except Exception as e:
                err = str(e)
                detail = "Invalid API key" if "authentication" in err.lower() or "401" in err else err[:120]
                return JSONResponse({"status": "error", "detail": detail})

        elif service == "tmdb":
            key = configs.get("tmdb_api_key")
            if not key:
                return JSONResponse({"status": "missing", "detail": "API key not configured"})
            status, body = await _http_get(f"https://api.themoviedb.org/3/configuration?api_key={key}")
            if status == 200:
                return JSONResponse({"status": "ok", "detail": "TMDB API key is valid"})
            elif status == 401:
                return JSONResponse({"status": "error", "detail": "Invalid API key"})
            else:
                return JSONResponse({"status": "error", "detail": f"HTTP {status}"})

        elif service == "lastfm":
            key = configs.get("lastfm_api_key")
            if not key:
                return JSONResponse({"status": "missing", "detail": "API key not configured"})
            status, body = await _http_get(
                "https://ws.audioscrobbler.com/2.0/",
                params={"method": "chart.gettopartists", "api_key": key, "format": "json", "limit": "1"},
            )
            if status == 200 and "artists" in body:
                return JSONResponse({"status": "ok", "detail": "Last.fm API key is valid"})
            elif body.get("error") == 10:
                return JSONResponse({"status": "error", "detail": "Invalid API key"})
            else:
                return JSONResponse({"status": "error", "detail": body.get("message", f"HTTP {status}")[:120]})

        elif service == "rawg":
            key = configs.get("rawg_api_key")
            if not key:
                return JSONResponse({"status": "missing", "detail": "API key not configured"})
            status, body = await _http_get(
                "https://api.rawg.io/api/games",
                params={"key": key, "page_size": "1"},
            )
            if status == 200 and "results" in body:
                return JSONResponse({"status": "ok", "detail": f"RAWG API key is valid ({body.get('count', '?')} games indexed)"})
            elif status == 401 or status == 403:
                return JSONResponse({"status": "error", "detail": "Invalid API key"})
            else:
                return JSONResponse({"status": "error", "detail": f"HTTP {status}"})

        elif service == "ytdlp":
            try:
                import yt_dlp
                ver = yt_dlp.version.__version__
                return JSONResponse({"status": "ok", "detail": f"yt-dlp {ver} installed"})
            except ImportError:
                return JSONResponse({"status": "error", "detail": "yt-dlp not installed — run: pip install yt-dlp"})

        else:
            return JSONResponse({"status": "error", "detail": f"Unknown service: {service}"}, status_code=400)

    # ------------------------------------------------------------------ #
    # Groups                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/groups", response_class=HTMLResponse)
    async def groups_list(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()

        async with get_session() as s:
            rows = list((
                await s.execute(select(GroupSettings).order_by(GroupSettings.chat_id))
            ).scalars().all())

        return templates.TemplateResponse(request, "groups.html", {
            "active": "groups",
            "groups": rows,
        })

    @app.get("/groups/search")
    async def groups_search(q: str = "", session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _bot:
            return JSONResponse({"error": "Bot not available"}, status_code=503)

        ref = _parse_chat_ref(q)
        if not ref:
            return JSONResponse({"error": "Invalid chat ID or link"}, status_code=400)

        try:
            chat_id_arg = int(ref) if ref.lstrip("-").isdigit() else ref
            chat = await _bot.get_chat(chat_id_arg)
        except Exception as e:
            return JSONResponse({"error": f"Could not find chat: {e}"}, status_code=404)

        bot_is_admin = False
        try:
            member = await _bot.get_chat_member(chat.id, _bot.id)
            bot_is_admin = member.status in ("administrator", "creator")
        except Exception:
            pass

        return JSONResponse({
            "chat_id": chat.id,
            "title": chat.title or str(chat.id),
            "username": chat.username or "",
            "type": chat.type,
            "bot_is_admin": bot_is_admin,
        })

    @app.post("/groups/{chat_id}/activate")
    async def group_activate(chat_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        await set_group_field(chat_id, is_active=True)
        return RedirectResponse(url=f"/groups/{chat_id}?saved=1", status_code=303)

    @app.post("/groups/{chat_id}/deactivate")
    async def group_deactivate(chat_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        await set_group_field(chat_id, is_active=False)
        return RedirectResponse(url=f"/groups/{chat_id}?saved=1", status_code=303)

    @app.get("/groups/{chat_id}", response_class=HTMLResponse)
    async def group_detail(
        chat_id: int,
        request: Request,
        saved: str = "",
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()

        g = await get_group_settings(chat_id)
        return templates.TemplateResponse(request, "group.html", {
            "active": "groups",
            "g": g,
            "saved": bool(saved),
        })

    @app.post("/groups/{chat_id}")
    async def group_update(
        chat_id: int,
        request: Request,
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()

        form = await request.form()

        bool_fields = [
            "welcome_enabled", "goodbye_enabled", "verification_enabled",
            "antispam_enabled", "antiad_enabled", "dlmode_enabled", "aichat_enabled",
        ]
        int_fields: dict[str, tuple[int, int]] = {
            "verification_timeout": (10, 600),
            "antispam_max_msgs": (1, 50),
            "antispam_window": (1, 300),
            "warn_limit": (1, 20),
        }
        text_fields = ["welcome_text", "goodbye_text", "rules_text"]

        kwargs: dict = {}
        for field in bool_fields:
            kwargs[field] = field in form
        for field, (lo, hi) in int_fields.items():
            try:
                kwargs[field] = max(lo, min(int(form.get(field, lo)), hi))
            except (ValueError, TypeError):
                pass
        for field in text_fields:
            val = form.get(field, "").strip()
            kwargs[field] = val if val else None

        await set_group_field(chat_id, **kwargs)
        return RedirectResponse(url=f"/groups/{chat_id}?saved=1", status_code=303)

    # ------------------------------------------------------------------ #
    # Blacklist                                                            #
    # ------------------------------------------------------------------ #

    @app.get("/blacklist", response_class=HTMLResponse)
    async def blacklist_page(
        request: Request,
        msg: str = "",
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()

        async with get_session() as s:
            rows = list((
                await s.execute(select(Blacklist).order_by(Blacklist.added_at.desc()))
            ).scalars().all())

        return templates.TemplateResponse(request, "blacklist.html", {
            "active": "blacklist",
            "entries": rows,
            "msg": msg,
        })

    @app.post("/blacklist/add")
    async def blacklist_add(
        user_id: int = Form(...),
        reason: str = Form(""),
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()
        await add_to_blacklist(user_id, reason=reason)
        return RedirectResponse(url="/blacklist?msg=added", status_code=303)

    @app.post("/blacklist/{user_id}/remove")
    async def blacklist_remove(user_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        await remove_from_blacklist(user_id)
        return RedirectResponse(url="/blacklist?msg=removed", status_code=303)

    # ------------------------------------------------------------------ #
    # RSS                                                                  #
    # ------------------------------------------------------------------ #

    @app.get("/rss", response_class=HTMLResponse)
    async def rss_page(
        request: Request,
        msg: str = "",
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()

        feeds = await get_all_rss_feeds()
        return templates.TemplateResponse(request, "rss.html", {
            "active": "rss",
            "feeds": feeds,
            "msg": msg,
        })

    @app.post("/rss/add")
    async def rss_add(
        chat_id: int = Form(...),
        url: str = Form(...),
        label: str = Form(""),
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()
        await add_rss_feed(chat_id=chat_id, url=url.strip(), label=label.strip())
        return RedirectResponse(url="/rss?msg=added", status_code=303)

    @app.post("/rss/{feed_id}/delete")
    async def rss_delete(feed_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        async with get_session() as s:
            from bot.database import RssSent
            from sqlalchemy import delete as sa_delete
            await s.execute(sa_delete(RssSent).where(RssSent.feed_id == feed_id))
            row = await s.get(RssFeed, feed_id)
            if row:
                await s.delete(row)
            await s.commit()
        return RedirectResponse(url="/rss?msg=deleted", status_code=303)

    @app.post("/rss/{feed_id}/pause")
    async def rss_pause(feed_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        async with get_session() as s:
            row = await s.get(RssFeed, feed_id)
            if row:
                row.paused = True
                await s.commit()
        return RedirectResponse(url="/rss", status_code=303)

    @app.post("/rss/{feed_id}/resume")
    async def rss_resume(feed_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        async with get_session() as s:
            row = await s.get(RssFeed, feed_id)
            if row:
                row.paused = False
                await s.commit()
        return RedirectResponse(url="/rss", status_code=303)

    # ------------------------------------------------------------------ #
    # Settings                                                             #
    # ------------------------------------------------------------------ #

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request,
        saved: str = "",
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()

        configs = await get_all_bot_configs()
        return templates.TemplateResponse(request, "settings.html", {
            "active": "settings",
            "configs": configs,
            "saved": bool(saved),
        })

    @app.post("/settings")
    async def settings_update(
        request: Request,
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()

        form = await request.form()
        api_keys = ["claude_api_key", "tmdb_api_key", "lastfm_api_key", "rawg_api_key",
                    "deepseek_api_key"]
        for key in api_keys:
            val = form.get(key, "").strip()
            if val:
                await set_bot_config(key, val)

        return RedirectResponse(url="/settings?saved=1", status_code=303)

    # ------------------------------------------------------------------ #
    # Media management page                                                #
    # ------------------------------------------------------------------ #

    @app.get("/media", response_class=HTMLResponse)
    async def media_page(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        configs = await get_all_bot_configs()
        platform_data = []
        for p in _MEDIA_PLATFORMS:
            has_cookie = bool(configs.get(p["cookie_key"])) if p["cookie_key"] else None
            platform_data.append({**p, "has_cookie": has_cookie})
        return templates.TemplateResponse(request, "media.html", {
            "active": "media",
            "platforms": platform_data,
        })

    @app.post("/media/cookie")
    async def media_cookie_update(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        key = (body.get("key") or "").strip()
        value = (body.get("value") or "").strip()
        allowed_keys = {p["cookie_key"] for p in _MEDIA_PLATFORMS if p["cookie_key"]}
        if key not in allowed_keys:
            return JSONResponse({"error": "unknown key"}, status_code=400)
        if value == "__CLEAR__":
            await delete_bot_config(key)
            return JSONResponse({"status": "cleared"})
        if not value:
            return JSONResponse({"status": "noop"})
        normalized, count = normalize_cookie(value)
        final = normalized if normalized else value
        await set_bot_config(key, final)
        return JSONResponse({"status": "saved", "pairs": count})

    # ------------------------------------------------------------------ #
    # PS Library                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/psn-library", response_class=HTMLResponse)
    async def psn_library_page(
        request: Request,
        tier: Optional[str] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        msg: str = "",
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()
        tier_int: Optional[int] = None
        if tier and tier.strip().isdigit():
            tier_int = int(tier.strip())
        status_val = status.strip() if status else None
        if status_val == "":
            status_val = None
        per_page = 50
        offset = (page - 1) * per_page
        games = await get_psn_library_games(
            tier=tier_int, status=status_val, keyword=q, limit=per_page, offset=offset
        )
        total = await count_psn_library_games(tier=tier_int, status=status_val, keyword=q)
        total_pages = max(1, (total + per_page - 1) // per_page)
        columns = await get_psn_columns()
        return templates.TemplateResponse(request, "psn_library.html", {
            "active": "psn_library",
            "games": games,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "filter_tier": tier_int,
            "filter_status": status_val,
            "filter_q": q or "",
            "msg": msg,
            "columns": columns,
        })

    @app.post("/psn-library/add")
    async def psn_library_add(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        form = await request.form()
        await _psn_lib_save_form(None, form)
        return RedirectResponse(url="/psn-library?msg=added", status_code=303)

    @app.post("/psn-library/{game_id}/edit")
    async def psn_library_edit(game_id: int, request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        form = await request.form()
        await _psn_lib_save_form(game_id, form)
        return RedirectResponse(url="/psn-library?msg=saved", status_code=303)

    @app.post("/psn-library/{game_id}/delete")
    async def psn_library_delete(game_id: int, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return _redirect_login()
        await delete_psn_game(game_id)
        return RedirectResponse(url="/psn-library?msg=deleted", status_code=303)

    @app.patch("/psn-library/{game_id}/cell")
    async def psn_library_patch_cell(
        game_id: int, request: Request, session: Optional[str] = Cookie(None)
    ):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        field = (body.get("field") or "").strip()
        value = body.get("value", "")
        if not field:
            return JSONResponse({"error": "field required"}, status_code=400)
        ok = await patch_psn_game_cell(game_id, field, str(value) if value is not None else "")
        if not ok:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"status": "saved"})

    @app.post("/psn-library/add-empty")
    async def psn_library_add_empty(session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from datetime import date
        row = await upsert_psn_game(
            title_en="",
            entry_date=date.today(),
            tier=2,
            status="active",
            region="HK",
        )
        return JSONResponse({"id": row.id if row else None})

    @app.get("/psn-library/columns")
    async def psn_library_get_columns(session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        cols = await get_psn_columns()
        return JSONResponse([{
            "col_key": c.col_key, "display_name": c.display_name,
            "col_type": c.col_type, "is_core": c.is_core,
            "visible": c.visible, "sort_order": c.sort_order,
        } for c in cols])

    @app.post("/psn-library/columns")
    async def psn_library_add_column(
        request: Request, session: Optional[str] = Cookie(None)
    ):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        col_key = (body.get("col_key") or "").strip()
        display_name = (body.get("display_name") or col_key).strip()
        col_type = (body.get("col_type") or "text").strip()
        if not col_key:
            return JSONResponse({"error": "col_key required"}, status_code=400)
        col = await upsert_psn_column(col_key, display_name=display_name,
                                      col_type=col_type, is_core=False)
        return JSONResponse({"col_key": col.col_key, "display_name": col.display_name})

    @app.patch("/psn-library/columns/{col_key}")
    async def psn_library_patch_column(
        col_key: str, request: Request, session: Optional[str] = Cookie(None)
    ):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        existing_cols = await get_psn_columns()
        existing = next((c for c in existing_cols if c.col_key == col_key), None)
        if not existing:
            return JSONResponse({"error": "not found"}, status_code=404)
        col = await upsert_psn_column(
            col_key,
            display_name=body.get("display_name", existing.display_name),
            col_type=body.get("col_type", existing.col_type),
            is_core=existing.is_core,
            visible=body.get("visible", existing.visible),
            sort_order=body.get("sort_order", existing.sort_order),
        )
        return JSONResponse({"col_key": col.col_key})

    @app.delete("/psn-library/columns/{col_key}")
    async def psn_library_delete_column(
        col_key: str, session: Optional[str] = Cookie(None)
    ):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        ok = await delete_psn_column(col_key)
        if not ok:
            return JSONResponse({"error": "not found or core column"}, status_code=400)
        return JSONResponse({"status": "deleted"})

    @app.post("/psn-library/bulk")
    async def psn_library_bulk(request: Request, session: Optional[str] = Cookie(None)):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        csv_text = body.get("csv", "")
        rows = _parse_psn_csv(csv_text)
        if not rows:
            return JSONResponse({"ok": 0, "err": 0})
        ok, err = await bulk_import_psn_games(rows)
        return JSONResponse({"ok": ok, "err": err})

    @app.post("/psn-library/import-ps-store")
    async def psn_library_import_ps_store(
        request: Request,
        file: UploadFile = File(...),
        clear_first: str = Form(""),
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        data = await file.read()
        if not data:
            return JSONResponse({"error": "empty file"}, status_code=400)

        rows, needs_translation = _parse_ps_store_csv_bytes(data)
        if not rows:
            return JSONResponse({"error": "no rows parsed"}, status_code=400)

        # AI batch-translate pure-Chinese titles (20 per batch)
        ai_translated = 0
        if needs_translation:
            deepseek_key = await get_bot_config("deepseek_api_key")
            claude_key   = await get_bot_config("claude_api_key")
            if deepseek_key or claude_key:
                batch_size = 20
                for batch_start in range(0, len(needs_translation), batch_size):
                    batch = needs_translation[batch_start:batch_start + batch_size]
                    names_list = "\n".join(f'{i+1}. {zh}' for i, (_, zh) in enumerate(batch))
                    prompt = (
                        f"Translate these {len(batch)} Chinese video game titles to English. "
                        f"Reply with ONLY a JSON array of strings in the same order:\n{names_list}"
                    )
                    try:
                        translated: List[str] = []
                        if deepseek_key:
                            async with aiohttp.ClientSession() as s:
                                async with s.post(
                                    "https://api.deepseek.com/chat/completions",
                                    headers={"Authorization": f"Bearer {deepseek_key}",
                                             "Content-Type": "application/json"},
                                    json={"model": "deepseek-chat",
                                          "messages": [{"role": "user", "content": prompt}],
                                          "max_tokens": 400, "temperature": 0},
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as r:
                                    if r.status == 200:
                                        resp = await r.json()
                                        text = resp["choices"][0]["message"]["content"].strip()
                                        text = _re.sub(r"```[a-z]*\n?", "", text).strip("` \n")
                                        translated = _json.loads(text)
                        elif claude_key:
                            import anthropic
                            client = anthropic.AsyncAnthropic(api_key=claude_key)
                            msg = await asyncio.wait_for(
                                client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=400,
                                    messages=[{"role": "user", "content": prompt}],
                                ),
                                timeout=30,
                            )
                            text = msg.content[0].text.strip()
                            text = _re.sub(r"```[a-z]*\n?", "", text).strip("` \n")
                            translated = _json.loads(text)

                        if isinstance(translated, list) and len(translated) == len(batch):
                            for j, (row_idx, _) in enumerate(batch):
                                en = str(translated[j]).strip()
                                if en:
                                    rows[row_idx]["title_en"] = en
                                    ai_translated += 1
                    except Exception:
                        pass  # Continue without translation for this batch

        if clear_first == "1":
            async with get_session() as s:
                from sqlalchemy import delete as _sa_delete
                from bot.database import PsnLibraryGame as _PG
                await s.execute(_sa_delete(_PG))
                await s.commit()

        ok, err = await bulk_import_psn_games(rows)
        return JSONResponse({"imported": ok, "errors": err, "ai_translated": ai_translated})

    @app.post("/settings/test-translate")
    async def settings_test_translate(
        request: Request,
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)

        deepseek_key = await get_bot_config("deepseek_api_key")
        claude_key   = await get_bot_config("claude_api_key")
        if not deepseek_key and not claude_key:
            return JSONResponse({"error": "no AI key configured"})

        prompt = (f"Reply with ONLY the official English title of this video game, "
                  f"nothing else: {text}")
        try:
            if deepseek_key:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        "https://api.deepseek.com/chat/completions",
                        headers={"Authorization": f"Bearer {deepseek_key}",
                                 "Content-Type": "application/json"},
                        json={"model": "deepseek-chat",
                              "messages": [{"role": "user", "content": prompt}],
                              "max_tokens": 30, "temperature": 0},
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            result = data["choices"][0]["message"]["content"].strip().strip('"').strip("'")
                            return JSONResponse({"result": result, "provider": "DeepSeek"})
            if claude_key:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=claude_key)
                msg = await asyncio.wait_for(
                    client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=30,
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=8,
                )
                result = msg.content[0].text.strip().strip('"').strip("'")
                return JSONResponse({"result": result, "provider": "Claude Haiku"})
        except Exception as e:
            return JSONResponse({"error": str(e)})
        return JSONResponse({"error": "translation failed"})

    # -----------------------------------------------------------------------
    # AI usage statistics
    # -----------------------------------------------------------------------

    @app.get("/ai-stats")
    async def ai_stats_page(
        request: Request,
        session: Optional[str] = Cookie(None),
    ):
        if not _authed(session):
            return _redirect_login()
        stats  = await get_ai_usage_stats()
        recent = await get_ai_usage_log(100)
        return templates.TemplateResponse(
            "ai_stats.html",
            {
                "request": request,
                "active":  "ai_stats",
                "stats":   stats,
                "recent":  recent,
            },
        )

    return app
