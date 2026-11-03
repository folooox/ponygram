"""FilmSpot template, channel gate, and Web Admin config defaults.

No network and no Telegram token. The channel id must stay empty until an
admin saves one; confirming a draft with no channel must not publish.
"""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from pathlib import Path

from bot.components import bool_config_checked
from modules.filmspot import (
    _COMPONENT,
    _DRAFTS,
    apply_selling_lines,
    confirm_keyboard,
    merge_media,
    message_chunks,
    parse_channel_id,
    parts_from_detail,
    poster_url_from_path,
    publishing_block_reason,
    publishing_enabled,
    render_filmspot,
    render_gossip,
    utf16_len,
)
from modules.movie import _format_movie

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
        self.assertIn("#科幻", caption)
        self.assertIn("#电影", caption)
        self.assertIn("#FilmSpot", caption)
        self.assertNotIn("#剧集", caption)
        self.assertLessEqual(utf16_len(caption), 1024)
        self.assertEqual(
            poster_url_from_path(parts["poster_path"]),
            "https://image.tmdb.org/t/p/w780/inception.jpg",
        )

    def test_missing_date_is_tbd_and_same_original_is_omitted(self) -> None:
        parts = parts_from_detail(
            _sample(release_date="", original_title="盗梦空间", tagline="", vote_count=0, popularity=0),
            "movie",
        )
        caption, _ = render_filmspot(parts)
        self.assertIn("档期待定", caption)
        self.assertNotIn("原名", caption)
        self.assertIn("待补充卖点", caption)

    def test_html_in_title_is_escaped(self) -> None:
        parts = parts_from_detail(_sample(title="<img src=x>", original_title="Plain"), "movie")
        caption, _ = render_filmspot(parts)
        self.assertNotIn("<img", caption)
        self.assertIn("&lt;img src=x&gt;", caption)

    def test_long_overview_stays_within_caption_limit(self) -> None:
        parts = parts_from_detail(_sample(overview="简介" * 3000), "movie")
        caption, followup = render_filmspot(parts)
        self.assertLessEqual(utf16_len(caption), 1024)
        self.assertIn("盗梦空间", caption)
        self.assertIn("…", caption)
        self.assertIn("卖点", caption + followup)
        self.assertIn("标签", caption + followup)

    def test_oversized_selling_points_split_to_followup(self) -> None:
        parts = parts_from_detail(_sample(), "movie")
        parts["selling"] = ["卖" * 500, "点" * 500, "啊" * 500]
        caption, followup = render_filmspot(parts)
        self.assertLessEqual(utf16_len(caption), 1024)
        self.assertIn("梗概", caption)
        self.assertIn("卖点", followup)
        self.assertIn("标签", followup)
        self.assertIn("#电影", followup)

    def test_tv_creators_and_tbd_schedule(self) -> None:
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
        parts = parts_from_detail(detail, "tv")
        caption, _ = render_filmspot(parts)
        self.assertIn("创作者 Duffer Brothers", caption)
        self.assertIn("主演 Millie Bobby Brown", caption)
        self.assertIn("档期待定", caption)
        self.assertIn("Stranger Things", caption)
        self.assertIn("#剧集", caption)
        self.assertNotIn("#电影", caption)

    def test_search_hit_without_genres_stays_pending_until_detail(self) -> None:
        search_hit = {"id": 1, "title": "Only Search", "genre_ids": [28], "overview": "x"}
        parts = parts_from_detail(merge_media(search_hit, None), "movie")
        caption, _ = render_filmspot(parts)
        self.assertIn("类型待补", caption)
        detail = {"genres": [{"name": "动作"}], "overview": "来自详情"}
        merged = merge_media(search_hit, detail)
        self.assertEqual(merged["genres"][0]["name"], "动作")
        self.assertEqual(merged["overview"], "来自详情")

    def test_empty_detail_overview_keeps_search_text(self) -> None:
        merged = merge_media(
            {"title": "A", "overview": "搜索简介"},
            {"overview": "  ", "genres": []},
        )
        self.assertEqual(merged["overview"], "搜索简介")

    def test_selling_lines_are_capped(self) -> None:
        lines = apply_selling_lines("• 第一\n- 第二\n3. 第三\n第四\n")
        self.assertEqual(lines, ["第一", "第二", "第三"])

    def test_gossip_escapes_and_splits(self) -> None:
        short, follow = render_gossip("今天的花边 <b>未证实</b>")
        self.assertEqual(follow, "")
        self.assertIn("花边", short)
        self.assertIn("&lt;b&gt;未证实&lt;/b&gt;", short)
        self.assertIn("#花边", short)
        self.assertIn("#FilmSpot", short)
        self.assertNotIn("<b>未证实</b>", short)
        long_caption, long_follow = render_gossip("闻" * 2000)
        self.assertLessEqual(utf16_len(long_caption), 1024)
        self.assertIn("#花边", long_caption + long_follow)
        joined = message_chunks(long_follow, limit=500)
        self.assertTrue(all(utf16_len(part) <= 500 for part in joined))
        self.assertIn("闻", "".join(joined))

    def test_short_lookup_is_not_the_channel_template(self) -> None:
        text = _format_movie(
            {"title": "盗梦空间", "overview": "短简介", "id": 1, "vote_average": 8, "vote_count": 10},
            "movie",
        )
        self.assertNotIn("卖点", text)
        self.assertNotIn("主创", text)
        self.assertIn("盗梦空间", text)


class ChannelGateTests(unittest.TestCase):
    def test_parse_never_invents_a_destination(self) -> None:
        self.assertIsNone(parse_channel_id(None))
        self.assertIsNone(parse_channel_id(""))
        self.assertIsNone(parse_channel_id("   "))
        self.assertIsNone(parse_channel_id("not a channel"))
        self.assertIsNone(parse_channel_id("@@bad"))
        self.assertEqual(parse_channel_id("@abcde"), "@abcde")
        self.assertEqual(parse_channel_id("abcde"), "@abcde")
        self.assertEqual(parse_channel_id("https://t.me/abcde"), "@abcde")
        self.assertEqual(parse_channel_id("-1001234567890"), -1001234567890)
        self.assertIsNone(parse_channel_id("https://t.me/abcde/99"))

    def test_unset_channel_blocks_send_and_allows_preview_gate(self) -> None:
        self.assertTrue(publishing_enabled(None))
        self.assertTrue(publishing_enabled(""))
        self.assertTrue(publishing_enabled("1"))
        self.assertFalse(publishing_enabled("0"))
        self.assertFalse(publishing_enabled("false"))

        missing = publishing_block_reason(None, None)
        self.assertIsNotNone(missing)
        assert missing is not None
        self.assertIn("filmspot_channel_id", missing)
        self.assertIn("Web 管理面板", missing)
        self.assertNotIn("关闭", missing)
        self.assertIsNone(re.search(r"-100\d{6,}", missing))

        invalid = publishing_block_reason("1", "???")
        assert invalid is not None
        self.assertIn("无法识别", invalid)

        disabled = publishing_block_reason("0", "@abcde")
        assert disabled is not None
        self.assertIn("关闭", disabled)
        self.assertNotIn("尚未配置", disabled)

        self.assertIsNone(publishing_block_reason(None, "@abcde"))
        self.assertIsNone(publishing_block_reason("1", "-1001234567890"))
        self.assertIsNone(publishing_block_reason("", "https://t.me/abcde"))

    def test_component_defaults_are_empty_channel(self) -> None:
        fields = {f.key: f for f in _COMPONENT.config_fields}
        self.assertEqual(fields["filmspot_channel_id"].kind, "text")
        self.assertEqual(fields["filmspot_channel_id"].default, "")
        self.assertEqual(fields["filmspot_enabled"].kind, "bool")
        self.assertEqual(fields["filmspot_enabled"].default, "1")
        blob = fields["filmspot_channel_id"].help + fields["filmspot_channel_id"].placeholder
        self.assertIsNone(re.search(r"-100\d{6,}", blob))
        self.assertFalse(bool_config_checked(None, ""))
        self.assertTrue(bool_config_checked(None, "1"))
        self.assertFalse(bool_config_checked("0", "1"))
        self.assertTrue(bool_config_checked("1", ""))

    def test_source_does_not_hardcode_a_channel(self) -> None:
        for relative in ("modules/filmspot.py", "modules/movie.py", ".env.example"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"-100\d{6,}", text),
                relative,
            )
        film = (ROOT / "modules/filmspot.py").read_text(encoding="utf-8")
        self.assertNotIn("os.getenv", film)
        self.assertNotIn("os.environ", film)
        movie = (ROOT / "modules/movie.py").read_text(encoding="utf-8")
        self.assertIn("生成频道稿", movie)
        self.assertIn("fs_make:", movie)
        self.assertIn("fs_make:", film)

    def test_confirm_buttons(self) -> None:
        keyboard = confirm_keyboard("ab12cd34", with_selling=True)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(labels, ["发送到频道", "取消", "改卖点"])
        self.assertTrue(all(item and len(item.encode()) <= 64 for item in data))


class PublishGuardTests(unittest.TestCase):
    def tearDown(self) -> None:
        _DRAFTS.clear()

    def test_confirm_without_channel_does_not_post(self) -> None:
        asyncio.run(self._confirm_without_channel())

    def test_confirm_posts_only_to_configured_channel(self) -> None:
        asyncio.run(self._confirm_posts_configured())

    async def _confirm_without_channel(self) -> None:
        from modules.filmspot import Draft, on_send, _store

        sent: list = []

        class Bot:
            async def send_photo(self, *args, **kwargs):
                sent.append(("photo", args, kwargs))

            async def send_message(self, *args, **kwargs):
                sent.append(("message", args, kwargs))

        message = _Message()
        query = _Query("fs_send:abcd1234", message)
        update = _Update(query, _User(7))
        context = _Context(Bot())
        draft = _store(Draft(
            token="abcd1234",
            owner_id=7,
            kind="film",
            caption="预览",
            followup="",
            poster_url="https://image.tmdb.org/t/p/w780/x.jpg",
            parts={"title": "预览"},
        ))
        self.assertEqual(draft.token, "abcd1234")

        async def fake_config(key: str):
            return None

        import bot.database as database
        original = database.get_bot_config
        database.get_bot_config = fake_config
        try:
            await on_send(update, context)
        finally:
            database.get_bot_config = original

        self.assertEqual(sent, [])
        self.assertTrue(message.replies)
        self.assertIn("filmspot_channel_id", message.replies[0])
        self.assertIn("abcd1234", _DRAFTS)

    async def _confirm_posts_configured(self) -> None:
        from modules.filmspot import Draft, on_send, _store

        class Bot:
            def __init__(self) -> None:
                self.sent: list = []

            async def send_photo(self, chat_id, photo, **kwargs):
                self.sent.append((chat_id, photo, kwargs.get("caption")))

            async def send_message(self, *args, **kwargs):
                raise AssertionError("unexpected message")

        bot = Bot()
        message = _Message()
        query = _Query("fs_send:abcd1234", message)
        update = _Update(query, _User(7))
        context = _Context(bot)
        _store(Draft(
            token="abcd1234",
            owner_id=7,
            kind="gossip",
            caption="🗞 花边",
            followup="",
            photo_file_id="file-1",
        ))

        async def fake_config(key: str):
            values = {
                "filmspot_enabled": "1",
                "filmspot_channel_id": "@abcde",
            }
            return values.get(key)

        import bot.database as database
        original = database.get_bot_config
        database.get_bot_config = fake_config
        try:
            await on_send(update, context)
        finally:
            database.get_bot_config = original

        self.assertEqual(bot.sent, [("@abcde", "file-1", "🗞 花边")])
        self.assertNotIn("abcd1234", _DRAFTS)
        self.assertTrue(any("已发送" in text for text in message.replies))
        self.assertTrue(query.cleared)


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Message:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class _Query:
    def __init__(self, data: str, message: _Message) -> None:
        self.data = data
        self.message = message
        self.cleared = False

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        return None

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        self.cleared = True


class _Update:
    def __init__(self, query: _Query, user: _User) -> None:
        self.callback_query = query
        self.effective_user = user
        self.effective_chat = None
        self.effective_message = query.message


class _Config:
    owner_id = 7

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_id


class _Context:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.bot_data = {"config": _Config()}


class WebConfigTests(unittest.TestCase):
    def test_component_form_leaves_channel_empty(self) -> None:
        db_path = "/tmp/ponygram-filmspot-test.db"
        try:
            os.remove(db_path)
        except OSError:
            pass
        asyncio.run(self._form(db_path))

    async def _form(self, db_path: str) -> None:
        import httpx
        from bot.components import register_component
        from bot.database import get_bot_config, init_db
        from web.app import create_web_app

        await init_db(f"sqlite+aiosqlite:///{db_path}")
        register_component(_COMPONENT)
        app = create_web_app("test-secret", bot=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/login",
                data={"password": "test-secret"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)
            page = await client.get("/components/filmspot")
            self.assertEqual(page.status_code, 200)
            body = page.text
            self.assertIn("filmspot_channel_id", body)
            self.assertRegex(body, r'name="filmspot_channel_id"\s+value=""')
            self.assertRegex(body, r'name="filmspot_enabled"[^>]*\bchecked\b')
            self.assertIsNone(re.search(r"-100\d{6,}", body))

            saved = await client.post(
                "/components/filmspot/config",
                data={"filmspot_channel_id": "@abcde", "filmspot_enabled": "on"},
                follow_redirects=False,
            )
            self.assertEqual(saved.status_code, 303)
            self.assertEqual(await get_bot_config("filmspot_channel_id"), "@abcde")
            self.assertEqual(await get_bot_config("filmspot_enabled"), "1")
            self.assertIsNone(publishing_block_reason(
                await get_bot_config("filmspot_enabled"),
                await get_bot_config("filmspot_channel_id"),
            ))

            again = await client.get("/components/filmspot")
            self.assertIn('value="@abcde"', again.text)

            off = await client.post(
                "/components/filmspot/config",
                data={"filmspot_channel_id": "@abcde"},
                follow_redirects=False,
            )
            self.assertEqual(off.status_code, 303)
            self.assertEqual(await get_bot_config("filmspot_enabled"), "0")
            reason = publishing_block_reason("0", "@abcde")
            assert reason is not None
            self.assertIn("关闭", reason)


if __name__ == "__main__":
    unittest.main()
