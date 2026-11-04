import asyncio
import html
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import feedparser
import httpx
from telegram.ext import Application
from sqlalchemy import select
from bot.database import init_db, get_session, set_bot_config, RssFeed, RssSent, User, upsert_user
from bot.plugin_loader import PluginLoader
from bot.router import registry
from bot import ai
from services import catalog
from modules import rss
from modules import catalog as telegram_catalog
from web.app import create_web_app

class RefactorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        await init_db('sqlite+aiosqlite:///' + self.tmp.name + '/test.db')
        await catalog.cache.clear()
        await ai._cache.clear()

    async def asyncTearDown(self):
        from bot import database
        await database._engine.dispose()
        self.tmp.cleanup()

    async def test_only_approved_modules_and_menu(self):
        registry._commands.clear()
        app = Application.builder().token('123:fake').build()
        loader = PluginLoader()
        await loader.load_modules(app, 'modules')
        self.assertEqual(loader.failed, [])
        self.assertEqual(set(loader.loaded), {'modules.catalog', 'modules.rss', 'modules.start'})
        self.assertEqual(set(registry.all_commands), {'start','help','movie','tv','music','book'})
        self.assertEqual(len(app.handlers[0]), 8)

    async def test_artist_removed(self):
        self.assertNotIn('artist',catalog.KINDS)
        with self.assertRaises(catalog.CatalogError):
            await catalog.search('artist','Singer')

    async def test_music_high_resolution_artwork(self):
        c=catalog._apple_card('music',{'trackId':1,'trackName':'Song','artworkUrl100':'https://is1-ssl.mzstatic.com/image/100x100bb.jpg'})
        self.assertTrue(c.image.endswith('/1200x1200bb.jpg'))
        self.assertEqual(catalog.apple_artwork('https://example.com/100x100bb.jpg'),'https://example.com/100x100bb.jpg')

    async def test_movie_native_chinese_full_text_and_source_facts(self):
        description='完整中文剧情介绍，保留每一个情节。'*50
        c=catalog.Card('movie','1','Film','TMDB','https://example.com',description=description)
        data={'overview':description,'runtime':169,'credits':{'crew':[{'name':'Nolan','job':'Director'}], 'cast':[{'name':'Actor'}]}}
        with patch.object(catalog,'request',AsyncMock(return_value=data)), patch.object(catalog,'polish_text',AsyncMock()) as polish:
            result=await catalog.detail(c)
        polish.assert_not_awaited()
        self.assertEqual(result.description,description)
        self.assertIn(description,catalog.render_card(result))
        self.assertEqual(dict(result.facts)['导演'],'Nolan')
        self.assertEqual(dict(result.facts)['主演'],'Actor')
        self.assertEqual(dict(result.facts)['时长'],'169 分钟')

    async def test_long_card_only_truncates_at_telegram_limit(self):
        c=catalog.Card('movie','1','Long film','TMDB','https://example.com',description='A & B '*2000)
        text=catalog.render_card(c)
        self.assertLessEqual(len(text),4000)
        self.assertTrue(text.endswith('</a>'))
        self.assertIn('…',text)
        self.assertNotIn(' & B',text)

    async def test_music_uses_apple_without_key(self):
        with patch.object(catalog, 'request', AsyncMock(return_value={'results':[{'trackId':1,'artistId':2,'trackName':'Song','artistName':'Singer','trackTimeMillis':201000}]})) as req:
            cards = await catalog.search('music','Song')
            detail = await catalog.detail(cards[0])
        self.assertIn('itunes.apple.com', req.call_args.args[0])
        self.assertEqual(dict(detail.facts)['时长'],'3:21')
        self.assertEqual(detail.source,'Apple Music')

    async def test_books_fallback_when_google_fails(self):
        async def request(url,params=None,**kwargs):
            if 'googleapis' in url: return None
            return {'docs':[{'key':'/works/OL1W','title':'Book','author_name':['Author'],'first_publish_year':1990}]}
        with patch.object(catalog,'request',request):
            cards=await catalog.search('book','Book')
        self.assertEqual(cards[0].source,'Open Library')

    async def test_short_chinese_book_query_is_supported(self):
        with patch.object(catalog,'request',AsyncMock(side_effect=[None,{'docs':[]}])) as request:
            await catalog.search('book','三体')
        self.assertEqual(request.call_args_list[1].args[1]['q'],'title:三体')

    async def test_inline_uses_same_card_without_external_writes(self):
        card=catalog.Card('music','1','Song','Apple Music','https://example.com',facts=[('歌手','Singer')])
        query=SimpleNamespace(query='music Song',answer=AsyncMock())
        with patch.object(catalog,'search',AsyncMock(return_value=[card])), patch.object(catalog,'detail',AsyncMock(return_value=card)):
            await telegram_catalog.inline(SimpleNamespace(inline_query=query),None)
        result=query.answer.call_args.args[0][0]
        self.assertEqual(result.input_message_content.message_text,catalog.render_card(card))

    async def test_books_only_merge_isbn_matches(self):
        google={'items':[{'id':'1','volumeInfo':{'title':'Book','industryIdentifiers':[{'identifier':'123'}]}}]}
        opened={'docs':[{'key':'/works/OL1W','title':'Book','isbn':['456']},{'key':'/works/OL2W','title':'Book','isbn':['123']}]}
        with patch.object(catalog,'request',AsyncMock(side_effect=[google,opened])):
            cards=await catalog.search('book','Book')
        self.assertEqual(len(cards),2)
        self.assertEqual(cards[0].raw['open_work'],'/works/OL2W')
        self.assertFalse(cards[0].image)

    async def test_movie_detail_and_tv_fields(self):
        c=catalog.Card('tv','1','Show','TMDB','https://example.com',raw={})
        with patch.object(catalog,'request',AsyncMock(return_value={'number_of_seasons':2,'number_of_episodes':20,'genres':[{'name':'Drama'}],'status':'Ended','vote_average':8.1,'vote_count':3})):
            d=await catalog.detail(c)
        self.assertEqual(dict(d.facts)['类型'],'Drama')
        self.assertEqual(dict(d.facts)['季数 / 集数'],'2 季 / 20 集')

    async def test_ai_fallback_no_key(self):
        self.assertEqual(await ai.polish_text('book','Original'), 'Original')

    async def test_ai_timeout_preserves_original(self):
        await set_bot_config('ai_enabled','1')
        await set_bot_config('deepseek_api_key','fake')
        with patch('bot.ai.aiohttp.ClientSession', side_effect=TimeoutError):
            self.assertEqual(await ai.polish_text('book','Original'),'Original')

    async def test_ai_cannot_change_source_facts(self):
        c=catalog.Card('music','1','Original song','Apple Music','https://example.com',description='English',raw={'artistName':'Singer'})
        with patch.object(catalog,'polish_text',AsyncMock(return_value='翻译简介')):
            d=await catalog.detail(c)
        self.assertEqual(d.title,'Original song')
        self.assertEqual(dict(d.facts)['歌手'],'Singer')
        self.assertEqual(d.description,'翻译简介')

    async def test_html_escaping_and_invalid_links(self):
        c=catalog.Card('book','1','<bad>&','Source','javascript:alert(1)',description='<b>Hi</b> & bye')
        text=catalog.render_card(c)
        self.assertNotIn('javascript:',text)
        self.assertIn('&amp;',text)
        self.assertNotIn('<bad>',text)

    async def test_one_result_still_requires_selection(self):
        status=SimpleNamespace(edit_text=AsyncMock())
        msg=SimpleNamespace(text='/music Song',reply_text=AsyncMock(return_value=status))
        update=SimpleNamespace(effective_message=msg,effective_user=SimpleNamespace(id=1))
        with patch.object(catalog,'search',AsyncMock(return_value=[catalog.Card('music','1','Song','Apple','https://example.com')])):
            await telegram_catalog.command(update,SimpleNamespace(args=['Song']))
        self.assertIn('reply_markup',status.edit_text.call_args.kwargs)

    async def test_foreign_callback_rejected(self):
        await telegram_catalog.selections.set('token',(1,[]))
        q=SimpleNamespace(data='catalog:token:0',from_user=SimpleNamespace(id=2),answer=AsyncMock())
        await telegram_catalog.selected(SimpleNamespace(callback_query=q),None)
        self.assertIn('自己的',q.answer.call_args.args[0])

    async def test_disabled_applies_to_all_searches(self):
        await set_bot_config('query_enabled_book','0')
        with self.assertRaises(catalog.CatalogError): await catalog.search('book','A')

    async def test_rss_rejects_personal_target(self):
        for value in ['12345','@ab','https://example.com']:
            with self.assertRaises(ValueError): rss.normalize_target(value)
        bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(type='private',id=1)))
        with self.assertRaises(ValueError): await rss.resolve_target(bot,'@person')
        self.assertEqual(rss.normalize_target('https://t.me/channel'),'@channel')

    async def test_rss_no_target_no_delivery(self):
        async with get_session() as s:
            s.add(RssFeed(chat_id=123,url='https://example.com/rss'))
            await s.commit()
        bot=SimpleNamespace(send_message=AsyncMock(),get_chat=AsyncMock())
        await rss._poll_feeds(SimpleNamespace(bot=bot))
        bot.send_message.assert_not_called()
        bot.get_chat.assert_not_called()

    async def test_rss_baseline_new_entries_and_dedup(self):
        async with get_session() as s:
            s.add(RssFeed(chat_id=-100,target='-100',url='https://example.com/rss'))
            await s.commit()
        bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(type='channel',id=-100)),send_message=AsyncMock())
        old=feedparser.FeedParserDict({'id':'old','title':'Old','link':'https://example.com/old'})
        new=feedparser.FeedParserDict({'id':'new','title':'New & fresh','link':'https://example.com/new'})
        parsed=feedparser.FeedParserDict({'feed':{'title':'Feed'},'entries':[old]})
        with patch.object(rss,'_fetch_feed',AsyncMock(return_value=parsed)):
            await rss._poll_feeds(SimpleNamespace(bot=bot))
            bot.send_message.assert_not_called()
            parsed.entries=[new,old]
            await rss._poll_feeds(SimpleNamespace(bot=bot))
            await rss._poll_feeds(SimpleNamespace(bot=bot))
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(bot.send_message.call_args.args[0],-100)
        self.assertIn('&amp;',bot.send_message.call_args.args[1])

    async def test_user_tracking_preserves_both_entry_points(self):
        user=SimpleNamespace(id=42,username='old_name',first_name='First',last_name=None,is_bot=False,language_code='zh')
        await upsert_user(user,source='private')
        user.username='new_name'
        user.language_code='en'
        await upsert_user(user,source='inline')
        async with get_session() as s:
            row=await s.get(User,42)
            self.assertEqual(row.username,'new_name')
            self.assertEqual(row.language_code,'en')
            self.assertIsNotNone(row.last_private_seen)
            self.assertIsNotNone(row.last_inline_seen)
            self.assertEqual((await s.execute(select(User))).scalars().all().__len__(),1)

    async def test_query_entry_points_record_actual_users(self):
        user=SimpleNamespace(id=43,username='query_user',first_name='Query',last_name=None,is_bot=False)
        update=SimpleNamespace(effective_user=user,effective_message=SimpleNamespace(text='/book',reply_text=AsyncMock()))
        await telegram_catalog.command(update,SimpleNamespace(args=[]))
        query=SimpleNamespace(query='',answer=AsyncMock())
        await telegram_catalog.inline(SimpleNamespace(effective_user=user,inline_query=query),None)
        async with get_session() as s:
            row=await s.get(User,43)
            self.assertIsNotNone(row.last_private_seen)
            self.assertIsNotNone(row.last_inline_seen)

    async def test_user_admin_search_filter_pagination_and_auth(self):
        from datetime import datetime
        await upsert_user(SimpleNamespace(id=42,username='search_me',first_name='<script>alert(1)</script>',is_bot=False),source='inline')
        async with get_session() as s:
            s.add_all([User(id=i,first_name='Legacy'+str(i)) for i in range(100,151)])
            await s.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_web_app('secret')),base_url='http://test') as client:
            self.assertEqual((await client.get('/users')).status_code,303)
            await client.post('/login',data={'password':'secret'})
            result=await client.get('/users',params={'q':'@search_me','entry':'inline'})
            self.assertEqual(result.status_code,200)
            self.assertIn('@search_me',result.text)
            self.assertIn('&lt;script&gt;',result.text)
            self.assertNotIn('<script>alert(1)</script>',result.text)
            result=await client.get('/users',params={'entry':'private'})
            self.assertIn('没有匹配的用户',result.text)
            result=await client.get('/users',params={'p':2})
            self.assertIn('第 2 / 2 页',result.text)
            result=await client.get('/users',params={'q':'%'})
            self.assertIn('没有匹配的用户',result.text)

    async def test_web_auth_removed_routes_and_rss_crud(self):
        app=create_web_app('test-secret')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            self.assertEqual((await client.get('/health')).status_code,401)
            await client.post('/login',data={'password':'test-secret'})
            for path in ['/','/queries','/rss','/settings','/ai-stats']:
                self.assertEqual((await client.get(path)).status_code,200,path)
            for path in ['/groups','/media','/psn-library','/composer','/components','/island']:
                self.assertEqual((await client.get(path)).status_code,404,path)
            r=await client.post('/rss/add',data={'url':'https://example.com/feed','target':'','label':'Feed','ai_enabled':'on'})
            self.assertEqual(r.status_code,303)
            async with get_session() as s:
                feed=(await s.execute(select(RssFeed))).scalar_one()
                self.assertIsNone(feed.target)
                self.assertTrue(feed.ai_enabled)
            r=await client.post(f'/rss/{feed.id}/edit',data={'url':feed.url,'target':'-100','label':'Edited'})
            self.assertEqual(r.status_code,303)
            await client.post(f'/rss/{feed.id}/delete')
            async with get_session() as s:
                self.assertIsNone(await s.get(RssFeed,feed.id))
            r=await client.post('/rss/add',data={'url':'https://example.com/feed','target':'123'})
            self.assertIn('不能是个人用户',r.text)
            r=await client.post('/settings',data={},headers={'Origin':'https://evil.test'})
            self.assertEqual(r.status_code,403)

if __name__=='__main__': unittest.main()
