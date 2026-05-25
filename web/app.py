"""
Ponygram web admin UI.

Start via main.py when WEB_ENABLED=true.  Provides:
  /           — dashboard
  /groups     — list all groups; search/activate new groups
  /groups/<id>— group settings form
  /blacklist  — view, add, remove global blacklist entries
  /rss        — manage RSS subscriptions across all chats
  /settings   — bot-level API key configuration
  /login      — password form (WEB_SECRET from .env)
  /logout     — clear session
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any, Optional

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
        text_fields = ["welcome_text", "goodbye_text"]

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

        from bot.database import delete_bot_config
        form = await request.form()
        api_keys = ["claude_api_key", "tmdb_api_key", "lastfm_api_key"]
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
