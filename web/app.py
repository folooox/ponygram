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
  /login      — password form (WEB_SECRET from .env)
  /logout     — clear session
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from pathlib import Path
from typing import Any, Optional

import aiohttp
from fastapi import Cookie, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bot.database import (
    Blacklist,
    GroupSettings,
    RssFeed,
    User,
    UserWarn,
    add_rss_feed,
    add_to_blacklist,
    delete_bot_config,
    get_all_bot_configs,
    get_all_rss_feeds,
    get_bot_config,
    get_group_settings,
    get_session,
    remove_from_blacklist,
    remove_rss_feed,
    set_bot_config,
    set_feed_paused,
    set_group_field,
)
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
# App factory
# ---------------------------------------------------------------------------

def create_web_app(secret: str, bot=None) -> FastAPI:
    global _secret, _bot
    _secret = secret
    _bot = bot

    app = FastAPI(title="Ponygram Admin", docs_url=None, redoc_url=None)

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
            "psn": {
                "label": "PSN (trophies)",
                "icon": "bi-person-badge",
                "status": "ok" if configs.get("psn_npsso") else "missing",
                "detail": "NPSSO token configured" if configs.get("psn_npsso") else "Not configured",
                "testable": bool(configs.get("psn_npsso")),
                "setup_url": "https://ca.account.sony.com/api/v1/ssocookie",
                "setup_hint": "Log in to store.playstation.com, then open ca.account.sony.com/api/v1/ssocookie and copy the npsso value",
                "format_hint": "64 alphanumeric chars",
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

        elif service == "psn":
            npsso = configs.get("psn_npsso")
            if not npsso:
                return JSONResponse({"status": "missing", "detail": "NPSSO token not configured"})
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        "https://ca.account.sony.com/api/authz/v3/oauth/token",
                        data={"grant_type": "sso_cookie", "npsso": npsso},
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Cookie": f"npsso={npsso}",
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        body = await r.json()
                        if r.status == 200 and body.get("access_token"):
                            return JSONResponse({"status": "ok", "detail": "NPSSO token is valid — access token obtained"})
                        elif r.status in (400, 401):
                            return JSONResponse({"status": "error", "detail": "NPSSO token is expired or invalid — please refresh it"})
                        else:
                            return JSONResponse({"status": "error", "detail": f"HTTP {r.status}: {str(body)[:100]}"})
            except Exception as e:
                return JSONResponse({"status": "error", "detail": str(e)[:120]})

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
        # Admin delete: bypass chat_id check by using a raw session delete
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
        api_keys = ["claude_api_key", "tmdb_api_key", "lastfm_api_key", "rawg_api_key", "psn_npsso"]
        cookie_keys = [
            "cookie_instagram", "cookie_twitter", "cookie_bilibili",
            "cookie_douyin", "cookie_tiktok", "cookie_kuaishou",
            "cookie_xiaohongshu", "cookie_youtube",
        ]
        for key in api_keys:
            val = form.get(key, "").strip()
            if val:
                await set_bot_config(key, val)
        for key in cookie_keys:
            val = form.get(key, "").strip()
            if val == "__CLEAR__":
                await delete_bot_config(key)
            elif val:
                await set_bot_config(key, val)

        return RedirectResponse(url="/settings?saved=1", status_code=303)

    return app
