"""FilmSpot template shared by /movie, /tv, and inline movie/tv.

No network. DeepSeek is optional: a missing polish result keeps derived bullets.
Music and book cards stay on the shared catalog renderer.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.ext import Application

from bot.database import init_db, set_bot_config
from bot.plugin_loader import PluginLoader
from bot.router import registry
from services import catalog
from services.filmspot import (
    _format_movie,
    apply_language_gaps,
    apply_selling_lines,
    card_text,
    choose_selling_points,
    derive_selling_points,
    merge_media,
    parts_from_detail,
    render_filmspot,
    utf16_len,
)
from modules import catalog as telegram_catalog
from web.app import create_web_app

ROOT = Path(__file__).resolve().parents[1]


def _sample(**overrides) -> dict:
    detail = {
        "id": 27205,
        "title": "盗梦空间",
        "original_title": "Inception",
        "genres": [{"name": "动作"}, {"name": "科幻"}, {"name": "冒险"}],
        "overview": "一名盗梦者进入他人梦境。",
        "release_date": "2010-07-16",
        "tagline": "你的思想不再是你的思想",
        "vote_average": 8.36,
        "vote_count": 36000,
        "popularity": 80,
        "poster_path": "/inception.jpg",
        "credits": {
            "crew": [
                {"name": "Christopher Nolan", "job": "Director"},
                {"name": "Someone", "job": "Writer"},
            ],
            "cast": [
                {"name": "Leonardo DiCaprio"},
                {"name": "Joseph Gordon-Levitt"},
                {"name": "Elliot Page"},
                {"name": "Extra Person"},
            ],
        },
    }
    detail.update(overrides)
    return detail


class FormatTests(unittest.TestCase):
    def test_movie_template_sections(self) -> None:
        parts = parts_from_detail(_sample(), "movie")
        caption, followup = render_filmspot(parts)
        self.assertEqual(followup, "")
        for label in ("片名", "原名", "类型", "主创", "档期", "梗概", "卖点", "标签"):
            self.assertIn(label, caption)
        self.assertIn("盗梦空间", caption)
        self.assertIn("Inception", caption)
        self.assertIn("动作 / 科幻 / 冒险", caption)
        self.assertIn("导演 Christopher Nolan", caption)
        self.assertIn("Leonardo DiCaprio", caption)
        self.assertNotIn("Extra Person", caption)
        self.assertNotIn("Someone", caption)
        self.assertIn("2010-07-16", caption)
        self.assertIn("你的思想不再是你的思想", caption)
        self.assertIn("8.4/10", caption)
        self.assertIn("#动作", caption)
        self.assertIn("#电影", caption)
        self.assertIn("#FilmSpot", caption)
        self.assertNotIn("#剧集", caption)
        self.assertLessEqual(utf16_len(caption), 1024)

    def test_missing_date_and_same_original(self) -> None:
        parts = parts_from_detail(
            _sample(release_date="", original_title="盗梦空间", tagline="", vote_count=0, popularity=0),
            "movie",
        )
        caption, _ = render_filmspot(parts)
        self.assertIn("档期待定", caption)
        self.assertNotIn("原名", caption)
        self.assertIn("待补充卖点", caption)

    def test_html_is_escaped(self) -> None:
        parts = parts_from_detail(_sample(title="<img src=x>", original_title="Plain"), "movie")
        caption, _ = render_filmspot(parts)
        self.assertNotIn("<img", caption)
        self.assertIn("&lt;img src=x&gt;", caption)

    def test_long_overview_stays_within_caption(self) -> None:
        parts = parts_from_detail(_sample(overview="简介" * 3000), "movie")
        caption, followup = render_filmspot(parts)
        self.assertLessEqual(utf16_len(caption), 1024)
        self.assertIn("盗梦空间", caption)
        self.assertIn("…", caption)
        joined = card_text(caption, followup)
        self.assertIn("卖点", joined)
        self.assertIn("标签", joined)
        self.assertLessEqual(utf16_len(joined), 4000)

    def test_oversized_selling_points_split(self) -> None:
        parts = parts_from_detail(_sample(), "movie")
        parts["selling"] = ["卖" * 500, "点" * 500, "啊" * 500]
        caption, followup = render_filmspot(parts)
        self.assertLessEqual(utf16_len(caption), 1024)
        self.assertIn("梗概", caption)
        self.assertIn("卖点", followup)
        self.assertIn("#电影", followup)

    def test_tv_template(self) -> None:
        detail = {
            "name": "怪奇物语",
            "original_name": "Stranger Things",
            "first_air_date": "",
            "overview": "小镇怪事。",
            "genres": [{"name": "剧情"}, {"name": "科幻"}],
            "created_by": [{"name": "Duffer Brothers"}],
            "credits": {"cast": [{"name": "Millie Bobby Brown"}], "crew": []},
            "vote_average": 8.6,
            "vote_count": 20000,
            "popularity": 100,
        }
        caption, _ = render_filmspot(parts_from_detail(detail, "tv"))
        self.assertIn("创作者 Duffer Brothers", caption)
        self.assertIn("主演 Millie Bobby Brown", caption)
        self.assertIn("档期待定", caption)
        self.assertIn("Stranger Things", caption)
        self.assertIn("#剧集", caption)
        self.assertNotIn("#电影", caption)

    def test_search_hit_without_genres_waits_for_details(self) -> None:
        search_hit = {"id": 1, "title": "Only Search", "genre_ids": [28], "overview": "搜索简介"}
        caption, _ = render_filmspot(parts_from_detail(merge_media(search_hit, None), "movie"))
        self.assertIn("类型待补", caption)
        merged = merge_media(search_hit, {"genres": [{"name": "动作"}], "overview": "  "})
        self.assertEqual(merged["genres"][0]["name"], "动作")
        self.assertEqual(merged["overview"], "搜索简介")

    def test_english_fills_only_empty_fields(self) -> None:
        zh = {
            "title": "盗梦空间",
            "overview": "中文简介",
            "genres": [{"name": "科幻"}],
            "credits": {"cast": [{"name": "演员"}], "crew": [{"name": "有人", "job": "Writer"}]},
        }
        en = {
            "title": "Inception",
            "overview": "English plot",
            "tagline": "Your mind",
            "genres": [{"name": "Action"}],
            "credits": {
                "cast": [{"name": "Leonardo"}],
                "crew": [{"name": "Nolan", "job": "Director"}],
            },
        }
        merged = apply_language_gaps(zh, en, "movie")
        self.assertEqual(merged["overview"], "中文简介")
        self.assertEqual(merged["genres"][0]["name"], "科幻")
        self.assertEqual(merged["tagline"], "Your mind")
        jobs = [row["job"] for row in merged["credits"]["crew"]]
        self.assertIn("Director", jobs)
        self.assertEqual(merged["credits"]["cast"][0]["name"], "演员")

    def test_derived_bullets_and_polish_fallback(self) -> None:
        points = derive_selling_points(_sample())
        self.assertEqual(points[0], "你的思想不再是你的思想")
        self.assertTrue(any(p.startswith("评分 ") for p in points))
        self.assertLessEqual(len(points), 3)
        self.assertEqual(choose_selling_points(None, points), points)
        self.assertEqual(choose_selling_points("   \n", points), points)
        self.assertEqual(choose_selling_points("• 梦境\n2. 潜行\n- 层数\n多余", points), ["梦境", "潜行", "层数"])
        self.assertEqual(apply_selling_lines(""), [])
        bare = derive_selling_points({"title": "X", "tagline": "", "vote_count": 0, "popularity": 0})
        self.assertEqual(bare, [])

    def test_format_without_context_does_not_call_out(self) -> None:
        item = _sample()
        text = asyncio.run(_format_movie(item, "movie", None))
        caption, followup = render_filmspot(parts_from_detail(item, "movie"))
        self.assertEqual(text, card_text(caption, followup))
        self.assertIn("片名", text)
        self.assertIn("卖点", text)

    def test_music_and_book_cards_keep_their_format(self) -> None:
        music = catalog.Card(
            "music", "1", "Song", "Apple Music", "https://example.com", facts=[("歌手", "Singer")],
        )
        self.assertEqual(
            catalog.render_card(music),
            "🎵 <b>Song</b>\n歌手：Singer\n\n<a href=\"https://example.com\">Apple Music</a>",
        )
        book = catalog.Card(
            "book", "1", "Book", "Open Library", "https://example.com",
            description="A plot.", facts=[("作者", "Author")],
        )
        self.assertEqual(
            catalog.render_card(book),
            "📚 <b>Book</b>\n作者：Author\n\nA plot.\n\n<a href=\"https://example.com\">Open Library</a>",
        )
        self.assertIsNone(catalog.film_caption(music))
        self.assertIsNone(catalog.film_caption(book))

    def test_no_new_commands_or_channel_admin(self) -> None:
        catalog_mod = (ROOT / "modules/catalog.py").read_text(encoding="utf-8")
        self.assertIn("registry.register_command(kind, command", catalog_mod)
        self.assertIn("catalog.detail(c)", catalog_mod)
        self.assertIn("catalog.render_card(card)", catalog_mod)
        forbidden = (
            "filmspot_channel",
            'register_command("filmspot"',
            'register_command("gossip"',
            'register_command("channel_post"',
            'register_command("channel_text"',
            "生成频道稿",
        )
        scanned = list((ROOT / "modules").glob("*.py"))
        scanned += [ROOT / "services/catalog.py", ROOT / "services/filmspot.py", ROOT / "web/app.py", ROOT / "modules/start.py"]
        for path in scanned:
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(name, text, path.name)
        self.assertFalse((ROOT / "modules/filmspot.py").exists())
        self.assertEqual(set(catalog.KINDS), {"movie", "tv", "music", "book"})
        settings = (ROOT / "web/templates/settings.html").read_text(encoding="utf-8")
        self.assertIn("deepseek_api_key", settings)
        self.assertIn("tmdb_api_key", settings)
        self.assertNotIn("filmspot_channel", settings)
        start = (ROOT / "modules/start.py").read_text(encoding="utf-8")
        self.assertNotIn("/filmspot", start)
        self.assertNotIn("/gossip", start)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        await init_db("sqlite+aiosqlite:///" + self.tmp.name + "/test.db")
        await catalog.cache.clear()

    async def asyncTearDown(self):
        from bot import database
        await database._engine.dispose()
        self.tmp.cleanup()

    async def test_menu_has_no_new_commands(self):
        registry._commands.clear()
        app = Application.builder().token("123:fake").build()
        loader = PluginLoader()
        await loader.load_modules(app, "modules")
        self.assertEqual(set(registry.all_commands), {"start", "help", "movie", "tv", "music", "book"})

    async def test_single_movie_sends_card_and_several_keep_the_picker(self):
        parts = parts_from_detail(_sample(), "movie")
        card = catalog.Card(
            "movie", "1", "盗梦空间", "TMDB", "https://example.com/m",
            image="https://image.tmdb.org/t/p/w500/inception.jpg", film=parts,
        )
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        msg = SimpleNamespace(text="/movie 盗梦空间", reply_text=AsyncMock(return_value=status), reply_photo=AsyncMock())
        update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=1))
        with patch.object(telegram_catalog, "upsert_user", AsyncMock()), \
             patch.object(catalog, "search", AsyncMock(return_value=[card])), \
             patch.object(catalog, "detail", AsyncMock(return_value=card)), \
             patch.object(catalog, "enabled", AsyncMock(return_value=True)):
            await telegram_catalog.command(update, SimpleNamespace(args=["盗梦空间"]))
        status.edit_text.assert_not_awaited()
        caption = msg.reply_photo.await_args.kwargs["caption"]
        self.assertIn("片名", caption)
        self.assertLessEqual(utf16_len(caption), 1024)

        other = catalog.Card("movie", "2", "其他", "TMDB", "https://example.com/o", film=parts)
        status.edit_text.reset_mock()
        msg.reply_photo.reset_mock()
        with patch.object(telegram_catalog, "upsert_user", AsyncMock()), \
             patch.object(catalog, "search", AsyncMock(return_value=[card, other])), \
             patch.object(catalog, "detail", AsyncMock()) as detail:
            await telegram_catalog.command(update, SimpleNamespace(args=["盗梦"]))
        detail.assert_not_awaited()
        self.assertIn("reply_markup", status.edit_text.await_args.kwargs)
        msg.reply_photo.assert_not_awaited()

    async def test_photo_followup_carries_selling_points(self):
        parts = parts_from_detail(_sample(), "movie")
        parts["selling"] = ["卖" * 500, "点" * 500, "啊" * 500]
        card = catalog.Card(
            "movie", "1", "盗梦空间", "TMDB", "https://example.com",
            image="https://image.tmdb.org/x.jpg", film=parts,
        )
        msg = SimpleNamespace(reply_photo=AsyncMock(), reply_text=AsyncMock())
        await telegram_catalog.send_card(msg, card)
        caption = msg.reply_photo.await_args.kwargs["caption"]
        self.assertLessEqual(utf16_len(caption), 1024)
        self.assertIn("梗概", caption)
        follow = msg.reply_text.await_args.args[0]
        self.assertIn("卖点", follow)
        self.assertIn("#电影", follow)

    async def test_inline_movie_inserts_the_same_card(self):
        parts = parts_from_detail(_sample(), "movie")
        card = catalog.Card("movie", "1", "盗梦空间", "TMDB", "https://example.com", film=parts)
        query = SimpleNamespace(query="movie 盗梦空间", answer=AsyncMock())
        with patch.object(catalog, "search", AsyncMock(return_value=[card])), \
             patch.object(catalog, "detail", AsyncMock(return_value=card)):
            await telegram_catalog.inline(SimpleNamespace(inline_query=query), None)
        text = query.answer.await_args.args[0][0].input_message_content.message_text
        self.assertEqual(text, catalog.render_card(card))
        self.assertIn("片名", text)
        self.assertIn("#FilmSpot", text)

    async def test_detail_prefers_zh_and_fills_english_gaps(self):
        zh = {
            "title": "盗梦空间",
            "original_title": "Inception",
            "overview": "中文简介",
            "genres": [{"name": "科幻"}],
            "release_date": "2010-07-16",
            "vote_average": 8.4,
            "vote_count": 100,
            "popularity": 40,
            "credits": {"cast": [{"name": "演员"}], "crew": [{"name": "诺兰", "job": "导演"}]},
        }
        en = {
            "title": "Inception",
            "overview": "English plot",
            "tagline": "Your mind",
            "genres": [{"name": "Action"}],
            "credits": {"cast": [{"name": "Leonardo"}], "crew": [{"name": "Nolan", "job": "Director"}]},
        }

        async def request(url, params=None, **kwargs):
            self.assertIn("/movie/7", url)
            lang = (params or {}).get("language")
            self.langs.append(lang)
            return zh if lang == "zh-CN" else en

        self.langs = []
        card = catalog.Card("movie", "7", "搜索名", "TMDB", "https://example.com", raw={"title": "搜索名"})
        with patch.object(catalog, "request", request):
            result = await catalog.detail(card)
        self.assertEqual(self.langs, ["zh-CN", "en-US"])
        text = catalog.render_card(result)
        self.assertIn("科幻", text)
        self.assertNotIn("Action", text)
        self.assertIn("中文简介", text)
        self.assertNotIn("English plot", text)
        self.assertIn("Your mind", text)
        self.assertIn("导演 诺兰", text)
        self.assertEqual(dict(result.facts)["导演"], "诺兰")

    async def test_selling_points_fall_back_when_deepseek_fails(self):
        await set_bot_config("deepseek_api_key", "fake")
        payload = _sample(id=7)
        card = catalog.Card("movie", "7", "盗梦空间", "TMDB", "https://example.com", raw=payload)
        with patch.object(catalog, "request", AsyncMock(return_value=payload)), \
             patch("services.filmspot.deepseek_call", AsyncMock(side_effect=RuntimeError("down"))):
            failed = await catalog.detail(card)
        failed_text = catalog.render_card(failed)
        self.assertIn("你的思想不再是你的思想", failed_text)
        self.assertIn("片名", failed_text)

        await catalog.cache.clear()
        with patch.object(catalog, "request", AsyncMock(return_value=payload)), \
             patch("services.filmspot.deepseek_call", AsyncMock(return_value="梦中梦\n层层嵌套")):
            polished = await catalog.detail(card)
        text = catalog.render_card(polished)
        self.assertIn("梦中梦", text)
        self.assertIn("层层嵌套", text)
        self.assertNotIn("你的思想不再是你的思想", text)

    async def test_admin_pages_keep_existing_keys(self):
        app = create_web_app("secret")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/login", data={"password": "secret"})
            settings = await client.get("/settings")
            queries = await client.get("/queries")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("卖点", settings.text)
        self.assertIn('name="deepseek_api_key"', settings.text)
        self.assertIn('name="tmdb_api_key"', settings.text)
        self.assertNotIn("filmspot_channel", settings.text)
        self.assertNotIn('name="filmspot', settings.text)
        self.assertIn("FilmSpot", queries.text)
        self.assertIn("音乐与书籍格式不变", queries.text)


if __name__ == "__main__":
    unittest.main()
