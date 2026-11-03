"""FilmSpot template shared by /movie, /tv, and inline movie/tv.

No network. DeepSeek is optional: a missing polish result keeps derived bullets.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from modules.movie import (
    _COMPONENT,
    _format_movie,
    apply_selling_lines,
    card_text,
    choose_selling_points,
    derive_selling_points,
    merge_media,
    parts_from_detail,
    render_filmspot,
    utf16_len,
)

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

    def test_no_new_commands_or_channel_admin(self) -> None:
        movie = (ROOT / "modules/movie.py").read_text(encoding="utf-8")
        self.assertIn('registry.register_command("movie"', movie)
        self.assertIn('registry.register_command("tv"', movie)
        for forbidden in (
            "filmspot_channel",
            "register_command(\"filmspot\"",
            "register_command(\"gossip\"",
            "register_command(\"channel_post\"",
            "register_command(\"channel_text\"",
            "生成频道稿",
        ):
            self.assertNotIn(forbidden, movie)
        self.assertFalse((ROOT / "modules/filmspot.py").exists())
        keys = [f.key for f in _COMPONENT.config_fields]
        self.assertEqual(keys, ["tmdb_api_key"])
        inline = (ROOT / "modules/inline.py").read_text(encoding="utf-8")
        self.assertIn("_format_movie(item, prefix, context)", inline)
        for name in ("book.py", "music.py"):
            text = (ROOT / "modules" / name).read_text(encoding="utf-8")
            self.assertNotIn("卖点", text)
            self.assertNotIn("filmspot", text.lower())
        start = (ROOT / "modules/start.py").read_text(encoding="utf-8")
        self.assertNotIn("/filmspot", start)
        self.assertNotIn("/gossip", start)


if __name__ == "__main__":
    unittest.main()
