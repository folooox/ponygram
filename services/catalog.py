"""Four catalog categories and one card pipeline for private and inline queries."""
from __future__ import annotations
import asyncio
import hashlib
import html
import re
import time
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlencode, urlparse
import aiohttp
from bot.cache import TTLCache
from bot.database import get_bot_config
from bot.ai import polish_text

KINDS = {'movie': '电影', 'tv': '剧集', 'music': '音乐', 'book': '书籍'}
ICONS = {'movie': '🎬', 'tv': '📺', 'music': '🎵', 'book': '📚'}
cache = TTLCache(ttl=3600, max_size=1000)
_locks: dict[str, asyncio.Lock] = {}
_last_request: dict[str, float] = {}

class CatalogError(Exception):
    pass

@dataclass
class Card:
    kind: str
    id: str
    title: str
    source: str
    url: str
    image: str = ''
    original: str = ''
    description: str = ''
    facts: list[tuple[str, str]] = field(default_factory=list)
    extra_links: list[tuple[str, str]] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)


def safe_url(value: str) -> str:
    value = str(value or '')
    return value if urlparse(value).scheme in ('http', 'https') else ''


def plain(value) -> str:
    return html.unescape(re.sub(r'<[^>]*>', '', str(value or ''))).strip()


async def request(url: str, params=None, *, strict=False):
    host = urlparse(url).hostname or ''
    # iTunes allows approximately 20 calls/minute; Open Library defaults to 1/s.
    interval = 3.1 if host == 'itunes.apple.com' else 1.05 if host == 'openlibrary.org' else 0
    lock = _locks.setdefault(host, asyncio.Lock())
    async with lock:
        if interval:
            await asyncio.sleep(max(0, interval - (time.monotonic() - _last_request.get(host, 0))))
        _last_request[host] = time.monotonic()
        try:
            async with aiohttp.ClientSession(headers={'User-Agent': 'Ponygram/2.0 (https://github.com/folooox/ponygram)'}) as s:
                async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        if strict:
                            raise CatalogError('数据源暂时不可用，请稍后重试。')
                        return None
                    return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            if strict:
                raise CatalogError('数据源连接失败，请稍后重试。')
            return None


def _tmdb_card(kind, item):
    image = f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get('poster_path') else ''
    return Card(kind, str(item['id']), item.get('title') or item.get('name') or '未命名', 'TMDB',
                f"https://www.themoviedb.org/{kind}/{item['id']}", image,
                item.get('original_title') or item.get('original_name') or '', item.get('overview') or '',
                facts=[('上映日期' if kind == 'movie' else '首播日期', item.get('release_date') or item.get('first_air_date') or ''),
                       ('评分', f"{item['vote_average']:.1f}/10" if item.get('vote_average') else '')], raw=item)


def apple_artwork(url: str) -> str:
    """Request a full-size image from Apple's image CDN, rather than the API thumbnail."""
    url = safe_url(url)
    if (urlparse(url).hostname or '').endswith('.mzstatic.com'):
        return re.sub(r'/[0-9]+x[0-9]+([^/]+)$', r'/1200x1200\1', url)
    return url


def _apple_card(kind, item):
    return Card(kind, str(item.get('trackId')), item.get('trackName') or '未命名', 'Apple Music',
                safe_url(item.get('trackViewUrl')), apple_artwork(item.get('artworkUrl100') or item.get('artworkUrl60')), raw=item)


def _google_card(item):
    v = item.get('volumeInfo', {})
    return Card('book', 'g:' + item['id'], v.get('title') or '未命名', 'Google Books',
                safe_url(v.get('infoLink')) or f"https://books.google.com/books?id={quote(item['id'])}",
                safe_url((v.get('imageLinks') or {}).get('thumbnail', '').replace('http://', 'https://')),
                description=plain(v.get('description')), facts=[('作者', '、'.join(v.get('authors', []))), ('出版社', v.get('publisher', '')),
                ('出版日期', v.get('publishedDate', '')), ('页数', str(v.get('pageCount') or '')), ('语言', v.get('language', ''))], raw=item)


def _open_card(item):
    cover = item.get('cover_i')
    return Card('book', 'o:' + item['key'].split('/')[-1], item.get('title') or '未命名', 'Open Library',
                'https://openlibrary.org' + item['key'], f'https://covers.openlibrary.org/b/id/{cover}-L.jpg' if cover else '',
                facts=[('作者', '、'.join(item.get('author_name', []))), ('首次出版年份', str(item.get('first_publish_year') or ''))], raw=item)


async def enabled(kind):
    # movie and tv share their provider, but can be enabled independently.
    return await get_bot_config('query_enabled_' + kind) != '0'


async def search(kind: str, query: str) -> list[Card]:
    if kind not in KINDS:
        raise CatalogError('不支持的查询类型。')
    if not await enabled(kind):
        raise CatalogError('该查询功能暂时停用。')
    query = query.strip()[:200]
    if not query:
        return []
    country = await get_bot_config('itunes_country') or 'HK'
    key = f'search:{kind}:{country}:{query.casefold()}'
    found = await cache.get(key)
    if found is not None:
        return found
    if kind in ('movie', 'tv'):
        api_key = await get_bot_config('tmdb_api_key')
        if not api_key:
            raise CatalogError('影视查询尚未配置，请联系管理员。')
        data = await request(f'https://api.themoviedb.org/3/search/{kind}',
                             {'api_key': api_key, 'query': query, 'language': 'zh-CN'}, strict=True)
        found = [_tmdb_card(kind, x) for x in data.get('results', [])[:5]]
    elif kind == 'music':
        data = await request('https://itunes.apple.com/search',
                             {'term': query, 'country': country, 'media': 'music',
                              'entity': 'song', 'limit': 5}, strict=True)
        found = [_apple_card(kind, x) for x in data.get('results', []) if x.get('trackId')][:5]
    else:
        google, opened = await asyncio.gather(
            request('https://www.googleapis.com/books/v1/volumes', {'q': query, 'maxResults': 5, 'printType': 'books'}),
            request('https://openlibrary.org/search.json', {'q': 'title:' + query if len(query) < 3 else query, 'limit': 5, 'lang': 'zh',
                    'fields': 'key,title,author_name,first_publish_year,cover_i,isbn,edition_key,language,editions,editions.key,editions.title,editions.language'}))
        if google is None and opened is None:
            raise CatalogError('书籍数据源暂时不可用，请稍后重试。')
        found = [_google_card(x) for x in (google or {}).get('items', [])]
        # Cross-source enrichment only through ISBN; never merge unrelated editions by title.
        for item in (opened or {}).get('docs', []):
            matches = [c for c in found if set(item.get('isbn', [])) & {
                i.get('identifier') for i in c.raw.get('volumeInfo', {}).get('industryIdentifiers', [])}]
            if matches:
                for c in matches:
                    c.raw['open_work'] = item['key']
                    c.extra_links.append(('Open Library', 'https://openlibrary.org' + item['key']))
            elif len(found) < 5:
                found.append(_open_card(item))
    await cache.set(key, found, ttl=3600 if found else 60)
    return found


async def _detail(card: Card, *, ai=True) -> Card:
    # Storefront is part of the cache key for region-specific music results.
    country = await get_bot_config('itunes_country') or 'HK'
    key = f'detail:{card.kind}:{card.id}:{country}'
    existing = await cache.get(key)
    if existing is not None:
        result = existing
    else:
        result = replace(card, facts=list(card.facts), extra_links=list(card.extra_links), raw=dict(card.raw))
        d = result.raw
        if card.kind in ('movie', 'tv'):
            api_key = await get_bot_config('tmdb_api_key')
            info = await request(f'https://api.themoviedb.org/3/{card.kind}/{card.id}', {'api_key': api_key or '', 'language': 'zh-CN', 'append_to_response': 'credits'})
            d = info or d
            result.description = d.get('overview') or result.description
            # Preserve native Chinese verbatim, even if the provider wrote a short synopsis.
            # Only fetch another language when the localized description is missing.
            if not result.description:
                original = await request(f'https://api.themoviedb.org/3/{card.kind}/{card.id}',
                                         {'api_key': api_key or '', 'language': 'en-US'})
                overview = (original or {}).get('overview') or ''
                if overview:
                    result.description = overview
            result.facts = [('上映日期' if card.kind == 'movie' else '首播日期', d.get('release_date') or d.get('first_air_date') or ''),
                            ('评分', f"{d['vote_average']:.1f}/10 · {d.get('vote_count', 0)} 人" if d.get('vote_average') else ''),
                            ('类型', '、'.join(x['name'] for x in d.get('genres', [])))]
            countries = '、'.join(x.get('name', '') for x in d.get('production_countries', []))
            language = '、'.join(x.get('english_name') or x.get('name') or '' for x in d.get('spoken_languages', []))
            cast = '、'.join(x.get('name', '') for x in d.get('credits', {}).get('cast', [])[:6])
            result.facts += [('地区', countries), ('语言', language)]
            if card.kind == 'movie':
                directors = '、'.join(x.get('name', '') for x in d.get('credits', {}).get('crew', []) if x.get('job') == 'Director')
                result.facts += [('时长', str(d['runtime']) + ' 分钟' if d.get('runtime') else ''), ('导演', directors)]
            else:
                creators = '、'.join(x.get('name', '') for x in d.get('created_by', []))
                runtime = d.get('episode_run_time', [])
                status = {'Ended': '已完结', 'Returning Series': '连载中', 'Canceled': '已取消',
                          'In Production': '制作中', 'Planned': '筹备中', 'Pilot': '试播'}.get(d.get('status'), d.get('status') or '')
                result.facts += [('季数 / 集数', f"{d.get('number_of_seasons', '—')} 季 / {d.get('number_of_episodes', '—')} 集" if d.get('number_of_seasons') else ''),
                                 ('播出状态', status), ('单集时长', ' / '.join(str(x) for x in runtime) + ' 分钟' if runtime else ''),
                                 ('主创', creators), ('播出平台', '、'.join(x.get('name', '') for x in d.get('networks', [])))]
            result.facts.append(('主演', cast))
        elif card.kind == 'music':
            duration = d.get('trackTimeMillis', 0) // 1000
            result.facts = [('歌手', d.get('artistName', '')), ('专辑', d.get('collectionName', '')),
                            ('时长', f'{duration // 60}:{duration % 60:02}' if duration else ''),
                            ('类型', d.get('primaryGenreName', '')), ('发行日期', d.get('releaseDate', '')[:10])]
        elif card.id.startswith('g:'):
            info = await request('https://www.googleapis.com/books/v1/volumes/' + quote(card.id[2:]))
            v = (info or d).get('volumeInfo', {})
            result.description = plain(v.get('description')) or result.description
            result.facts = [('作者', '、'.join(v.get('authors', []))), ('出版社', v.get('publisher', '')),
                            ('出版日期', v.get('publishedDate', '')), ('页数', str(v.get('pageCount') or '')),
                            ('语言', v.get('language', ''))]
            if not result.description and d.get('open_work'):
                work = await request('https://openlibrary.org' + d['open_work'] + '.json')
                desc = (work or {}).get('description', '')
                result.description = plain(desc.get('value', '') if isinstance(desc, dict) else desc)
        else:
            work = await request(result.url + '.json')
            desc = (work or {}).get('description', '')
            result.description = plain(desc.get('value', '') if isinstance(desc, dict) else desc)
            result.facts = [('作者', '、'.join(d.get('author_name', []))),
                            ('首次出版年份', str(d.get('first_publish_year') or ''))]
            # Search's matched edition belongs to this work. Use all edition facts together,
            # rather than combining publisher/pages from different translations.
            editions = d.get('editions', {}).get('docs', [])
            edition_key = editions[0].get('key') if editions else None
            if edition_key:
                edition = await request('https://openlibrary.org' + edition_key + '.json')
                if edition:
                    result.title = edition.get('title') or result.title
                    result.url = 'https://openlibrary.org' + edition_key
                    result.description = plain(edition.get('description', {}).get('value', '') if isinstance(edition.get('description'), dict) else edition.get('description')) or result.description
                    result.facts += [('出版社', '、'.join(edition.get('publishers', []))),
                                     ('出版日期', edition.get('publish_date') or ''),
                                     ('页数', str(edition.get('number_of_pages') or '')),
                                     ('语言', '、'.join(x.get('key', '').split('/')[-1] for x in edition.get('languages', [])))]
                    covers = edition.get('covers', [])
                    if covers and covers[0] > 0:
                        result.image = f'https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg'
        if card.kind == 'book':
            result.extra_links.append(('Goodreads 搜索', 'https://www.goodreads.com/search?' + urlencode({'q': result.title})))
        result.raw = {}  # downstream rendering sees only normalized source facts
        await cache.set(key, result)
    if ai and result.description and (card.kind not in ('movie', 'tv') or not re.search(r'[\u4e00-\u9fff]', result.description)):
        polished = await polish_text(card.kind, result.description)
        return replace(result, description=polished)
    return result


def render_card(card: Card) -> str:
    esc = lambda value: html.escape(plain(value))
    lines = [f'{ICONS[card.kind]} <b>{esc(card.title[:160])}</b>']
    if card.original and card.original != card.title:
        lines.append(f'<i>{esc(card.original[:160])}</i>')
    for label, value in card.facts:
        if value:
            lines.append(f'{esc(label)}：{esc(str(value)[:260])}')
    links = [(card.source, card.url)] + card.extra_links
    footer = '\n\n' + ' · '.join(f'<a href="{html.escape(safe_url(url), quote=True)}">{esc(label)}</a>'
                                      for label, url in links if safe_url(url))
    header = '\n'.join(lines)
    description = plain(card.description)
    # Preserve the complete synopsis whenever it fits Telegram's 4096-character message.
    # Limit escaped HTML safely; never slice through tags or an entity.
    budget = max(0, 4000 - len(header) - len(footer) - 2)
    if len(html.escape(description)) > budget:
        lo, hi = 0, len(description)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(html.escape(description[:mid])) + 1 <= budget:
                lo = mid
            else:
                hi = mid - 1
        description = description[:lo].rstrip() + '…' if lo else ''
    return header + ('\n\n' + html.escape(description) if description else '') + footer


async def detail(card: Card, *, ai=True) -> Card:
    """Bounded enrichment shared by every entry point; search facts survive timeouts."""
    try:
        return await asyncio.wait_for(_detail(card, ai=ai), timeout=10)
    except TimeoutError:
        fallback = replace(card, facts=list(card.facts), extra_links=list(card.extra_links))
        if fallback.kind == 'book' and not any(label == 'Goodreads 搜索' for label, _ in fallback.extra_links):
            fallback.extra_links.append(('Goodreads 搜索', 'https://www.goodreads.com/search?' + urlencode({'q': fallback.title})))
        return fallback
