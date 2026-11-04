"""Ponygram v2 admin: catalog, global AI, RSS and runtime status."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Request, Cookie, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, delete, or_, cast, String
from bot.database import (User, RssFeed, RssSent, get_session, get_bot_config, set_bot_config,
                          get_all_rss_feeds, get_ai_usage_log, get_ai_usage_stats)
from bot.ai import DEFAULT_PROMPT, polish_text
from services import catalog, notify
from modules.rss import normalize_target, resolve_target, _fetch_feed

templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))


def create_web_app(secret, bot=None):
    app = FastAPI(title='Ponygram Admin', docs_url=None, redoc_url=None)
    started = time.time()

    def authed(token):
        try:
            stamp, nonce, sig = (token or '').split('.')
            expected = hmac.new(secret.encode(), f'{stamp}.{nonce}'.encode(), hashlib.sha256).hexdigest()
            return 0 <= time.time() - int(stamp) < 7 * 86400 and secrets.compare_digest(sig, expected)
        except (ValueError, TypeError):
            return False

    def require(session):
        if not authed(session):
            raise HTTPException(401, '请先登录')

    def page(request, name, **context):
        return templates.TemplateResponse(request, name + '.html', {'active': name, **context})

    def redirect(path):
        return RedirectResponse(path, status_code=303)

    @app.middleware('http')
    async def protect(request, call_next):
        path = request.url.path
        if path not in ('/login', '/logout') and not authed(request.cookies.get('session')):
            if request.method == 'GET' and path not in ('/health',):
                return redirect('/login')
            return JSONResponse({'error': '请先登录'}, status_code=401)
        # Reject cross-origin browser writes while supporting plain forms and server-side clients.
        origin = request.headers.get('origin')
        if request.method == 'POST' and origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'error': '无效的请求来源'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/login')
    async def login_page(request: Request):
        return page(request, 'login', error=None)

    @app.post('/login')
    async def login(request: Request):
        form = await request.form()
        if not secrets.compare_digest(str(form.get('password') or ''), secret):
            return page(request, 'login', error='密码不正确')
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        sig = hmac.new(secret.encode(), f'{stamp}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        response = redirect('/')
        response.set_cookie('session', f'{stamp}.{nonce}.{sig}', httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', max_age=7 * 86400)
        return response

    @app.get('/logout')
    async def logout():
        response = redirect('/login')
        response.delete_cookie('session')
        return response

    @app.get('/')
    async def dashboard(request: Request):
        async with get_session() as s:
            users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        feeds = await get_all_rss_feeds()
        return page(request, 'dashboard', user_count=users, feeds=feeds, uptime=int(time.time() - started),
                    kinds=catalog.KINDS, bot_username=getattr(bot, 'username', '') if bot else '')

    @app.get('/users')
    async def users(request: Request, q: str = '', entry: str = '', p: int = 1):
        q = q.strip()[:100]
        conditions = []
        if q:
            pattern = '%' + q.lstrip('@').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            conditions.append(or_(cast(User.id, String).ilike(pattern, escape='\\'),
                                  User.username.ilike(pattern, escape='\\'),
                                  User.first_name.ilike(pattern, escape='\\'),
                                  User.last_name.ilike(pattern, escape='\\')))
        if entry == 'private':
            conditions.append(User.last_private_seen.is_not(None))
        elif entry == 'inline':
            conditions.append(User.last_inline_seen.is_not(None))
        elif entry == 'historical':
            conditions += [User.last_private_seen.is_(None), User.last_inline_seen.is_(None)]
        else:
            entry = ''
        async with get_session() as s:
            total = (await s.execute(select(func.count()).select_from(User).where(*conditions))).scalar_one()
            pages = max(1, (total + 49) // 50)
            p = min(max(1, p), pages)
            rows = list((await s.execute(select(User).where(*conditions)
                         .order_by(User.last_seen.desc(), User.id.desc()).offset((p - 1) * 50).limit(50))).scalars())
        from urllib.parse import urlencode
        return page(request, 'users', users=rows, total=total, q=q, entry=entry, current_page=p, pages=pages,
                    previous_url='/users?' + urlencode({'q': q, 'entry': entry, 'p': p - 1}),
                    next_url='/users?' + urlencode({'q': q, 'entry': entry, 'p': p + 1}))

    @app.get('/health')
    async def health():
        return {'telegram': {'label': 'Telegram', 'status': 'running' if bot else 'preview',
                             'detail': '机器人已连接' if bot else '独立预览，无机器人连接'},
                'tmdb': {'label': 'TMDB', 'status': 'configured' if await get_bot_config('tmdb_api_key') else 'missing'},
                'itunes': {'label': 'iTunes Search', 'status': 'no_key_required'},
                'books': {'label': 'Google Books + Open Library', 'status': 'no_key_required'},
                'ai': {'label': '全局 AI', 'status': 'enabled' if await get_bot_config('ai_enabled') == '1' and await get_bot_config('deepseek_api_key') else 'disabled'}}

    @app.get('/queries')
    async def queries(request: Request):
        return page(request, 'queries', kinds=catalog.KINDS,
                    enabled={k: await catalog.enabled(k) for k in catalog.KINDS})

    @app.post('/queries/toggle')
    async def toggle(request: Request):
        form = await request.form()
        kind = form.get('kind')
        if kind not in catalog.KINDS:
            raise HTTPException(400, '未知类型')
        await set_bot_config('query_enabled_' + kind, '1' if form.get('enabled') == '1' else '0')
        return redirect('/queries')

    @app.post('/queries/test')
    async def test_query(request: Request):
        form = await request.form()
        kind, query = str(form.get('kind') or ''), str(form.get('query') or '')
        results, cards, error = [], [], ''
        try:
            results = await asyncio.wait_for(catalog.search(kind, query), timeout=12)
            if results:
                c = await asyncio.wait_for(catalog.detail(results[0]), timeout=20)
                cards = [{'title': c.title, 'image': c.image, 'html': catalog.render_card(c), 'url': c.url}]
        except (catalog.CatalogError, TimeoutError) as e:
            error = str(e) or '查询超时'
        return page(request, 'queries', kinds=catalog.KINDS,
                    enabled={k: await catalog.enabled(k) for k in catalog.KINDS}, results=results, cards=cards, error=error,
                    selected_kind=kind, query=query)

    async def render_settings(request, saved='', notify_status='', notify_error='', form_chat_id=None, form_label=None):
        values = {k: await get_bot_config(k) or '' for k in ['itunes_country', 'ai_enabled', 'ai_model', 'ai_prompt']}
        if form_chat_id is None:
            chat_id = await get_bot_config(notify.NOTIFY_CHAT_ID_KEY) or ''
        else:
            chat_id = form_chat_id
        if form_label is None:
            label = await get_bot_config(notify.NOTIFY_LABEL_KEY) or ''
        else:
            label = form_label
        if notify_status not in ('', 'saved', 'sent'):
            notify_status = ''
        return page(request, 'settings', values=values, prompt=DEFAULT_PROMPT, saved=saved,
                    tmdb_configured=bool(await get_bot_config('tmdb_api_key')),
                    ai_configured=bool(await get_bot_config('deepseek_api_key')),
                    notify_chat_id=chat_id, notify_label=label, notify=notify_status, notify_error=notify_error)

    @app.get('/settings')
    async def settings(request: Request, saved: str = '', notify: str = ''):
        return await render_settings(request, saved=saved, notify_status=notify)

    @app.post('/settings')
    async def save_settings(request: Request):
        form = await request.form()
        country = str(form.get('itunes_country') or 'HK').upper()
        if country not in ['HK', 'TW', 'CN', 'US', 'JP', 'GB']:
            raise HTTPException(400, '不支持的商店地区')
        for key in ['tmdb_api_key', 'deepseek_api_key']:
            if form.get('clear_' + key):
                await set_bot_config(key, '')
            elif str(form.get(key) or '').strip():
                await set_bot_config(key, str(form[key]).strip())
        await set_bot_config('itunes_country', country)
        await set_bot_config('ai_enabled', '1' if form.get('ai_enabled') else '0')
        await set_bot_config('ai_model', str(form.get('ai_model') or 'deepseek-chat')[:100])
        await set_bot_config('ai_prompt', str(form.get('ai_prompt') or DEFAULT_PROMPT)[:4000])
        await catalog.cache.clear()
        return redirect('/settings?saved=1')

    @app.post('/settings/notify')
    async def save_notify(request: Request):
        form = await request.form()
        raw_chat = str(form.get('notify_chat_id') or '')
        label = notify.normalize_label(form.get('notify_label'))
        try:
            chat_id = notify.parse_chat_id(raw_chat)
        except ValueError as exc:
            return await render_settings(request, notify_error=str(exc), form_chat_id=raw_chat.strip(), form_label=label)
        await set_bot_config(notify.NOTIFY_CHAT_ID_KEY, str(chat_id))
        await set_bot_config(notify.NOTIFY_LABEL_KEY, label)
        return redirect('/settings?notify=saved')

    @app.post('/settings/notify/test')
    async def test_notify(request: Request):
        # Same session cookie and origin check as the other admin writes.
        try:
            await notify.send_text(notify.TEST_PING, bot=bot)
        except notify.NotifyError as exc:
            return await render_settings(request, notify_error=str(exc))
        return redirect('/settings?notify=sent')

    @app.post('/settings/test-ai')
    async def test_ai(request: Request):
        if not await get_bot_config('deepseek_api_key'):
            return {'ok': False, 'error': '未配置 AI Key'}
        text = str((await request.form()).get('text') or 'A quiet story about friendship and hope.')[:1500]
        result = await polish_text('admin', text, force=True)
        return {'ok': result != text, 'text': result, 'fallback': result == text}

    @app.get('/ai-stats')
    async def stats(request: Request):
        return page(request, 'ai_stats', stats=await get_ai_usage_stats(), recent=await get_ai_usage_log())

    @app.get('/rss')
    async def rss(request: Request, msg: str = ''):
        return page(request, 'rss', feeds=await get_all_rss_feeds(), msg=msg, error='')

    async def save_feed(request, feed_id=None):
        form = await request.form()
        try:
            url = str(form.get('url') or '').strip()
            if urlparse(url).scheme not in ('http', 'https') or not urlparse(url).hostname:
                raise ValueError('请输入有效的 HTTP/HTTPS 订阅地址。')
            target = normalize_target(form.get('target'))
            if target and bot:
                try:
                    await resolve_target(bot, target)
                except Exception:
                    raise ValueError('无法访问该频道或群组。请检查目标，并先将机器人加入其中。')
            async with get_session() as s:
                row = await s.get(RssFeed, feed_id) if feed_id else RssFeed(chat_id=0)
                if row is None:
                    raise HTTPException(404, '订阅不存在')
                if row.url != url and feed_id:
                    await s.execute(delete(RssSent).where(RssSent.feed_id == row.id))
                    row.last_fetched = None
                row.url, row.target = url, target or None
                row.label = str(form.get('label') or '').strip()[:128]
                row.ai_enabled = bool(form.get('ai_enabled'))
                row.paused = bool(form.get('paused'))
                row.chat_id = int(target) if target and target.startswith('-') else 0
                s.add(row)
                await s.commit()
            return redirect('/rss?msg=saved')
        except ValueError as e:
            return page(request, 'rss', feeds=await get_all_rss_feeds(), error=str(e), msg='')

    @app.post('/rss/add')
    async def add_feed(request: Request):
        return await save_feed(request)

    @app.post('/rss/{feed_id}/edit')
    async def edit_feed(feed_id: int, request: Request):
        return await save_feed(request, feed_id)

    @app.post('/rss/{feed_id}/delete')
    async def delete_feed(feed_id: int):
        async with get_session() as s:
            await s.execute(delete(RssSent).where(RssSent.feed_id == feed_id))
            await s.execute(delete(RssFeed).where(RssFeed.id == feed_id))
            await s.commit()
        return redirect('/rss?msg=deleted')

    @app.post('/rss/test')
    async def test_feed(request: Request):
        url = str((await request.form()).get('url') or '')
        data = await _fetch_feed(url)
        return {'ok': data is not None, 'title': data.feed.get('title', '') if data else '',
                'entries': len(data.entries) if data else 0}

    return app
