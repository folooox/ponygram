import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from telegram.error import TelegramError

from bot.database import delete_bot_config, get_bot_config, init_db, set_bot_config
from services import notify
from web.app import create_web_app


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        notify.bind(None)
        await init_db("sqlite+aiosqlite:///" + self.tmp.name + "/test.db")

    async def asyncTearDown(self):
        notify.bind(None)
        from bot import database
        await database._engine.dispose()
        self.tmp.cleanup()

    async def test_startup_seeds_default_chat_once(self):
        destination = await notify.get_destination()
        self.assertEqual(destination.chat_id, int(notify.DEFAULT_NOTIFY_CHAT_ID))
        self.assertEqual(destination.label, "")
        await set_bot_config(notify.NOTIFY_CHAT_ID_KEY, "-1001")
        await set_bot_config(notify.NOTIFY_LABEL_KEY, "ops")
        await notify.ensure_notify_config()
        destination = await notify.get_destination()
        self.assertEqual(destination.chat_id, -1001)
        self.assertEqual(destination.label, "ops")
        await delete_bot_config(notify.NOTIFY_CHAT_ID_KEY)
        await notify.ensure_notify_config()
        self.assertEqual((await notify.get_destination()).chat_id, int(notify.DEFAULT_NOTIFY_CHAT_ID))

    def test_parse_chat_id(self):
        self.assertEqual(notify.parse_chat_id("  -1003998603559 "), -1003998603559)
        self.assertEqual(notify.parse_chat_id("123456789"), 123456789)
        self.assertEqual(notify.parse_chat_id("-100"), -100)
        self.assertEqual(notify.parse_chat_id(str(2**63 - 1)), 2**63 - 1)
        for bad in ["", " ", "abc", "12.5", "0", "-0", "--1", "@channel", "+100", "1e10", " -100 1", "00"]:
            with self.assertRaises(ValueError):
                notify.parse_chat_id(bad)
        with self.assertRaises(ValueError):
            notify.parse_chat_id(str(2**63))
        with self.assertRaises(ValueError):
            notify.parse_chat_id("9" * 19)

    async def test_send_text_uses_configured_chat(self):
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
        message_id = await notify.send_text("结算完成", bot=bot)
        self.assertEqual(message_id, 7)
        self.assertEqual(bot.send_message.await_args.args, (int(notify.DEFAULT_NOTIFY_CHAT_ID), "结算完成"))
        self.assertEqual(bot.send_message.await_args.kwargs["parse_mode"], None)
        self.assertTrue(bot.send_message.await_args.kwargs["disable_web_page_preview"])

    async def test_missing_or_invalid_chat_does_not_send(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        await set_bot_config(notify.NOTIFY_CHAT_ID_KEY, "")
        with self.assertRaisesRegex(notify.NotifyError, "未配置"):
            await notify.send_text("hi", bot=bot)
        await set_bot_config(notify.NOTIFY_CHAT_ID_KEY, "nope")
        with self.assertRaisesRegex(notify.NotifyError, "Chat ID"):
            await notify.send_text("hi", bot=bot)
        with self.assertRaisesRegex(notify.NotifyError, "为空"):
            await notify.send_text("  ", bot=bot)
        with self.assertRaisesRegex(notify.NotifyError, "4096"):
            await notify.send_text("x" * 4097, bot=bot)
        bot.send_message.assert_not_called()

    async def test_no_bot_is_a_clear_error(self):
        with self.assertRaisesRegex(notify.NotifyError, "尚未连接"):
            await notify.send_text("hi")

    async def test_telegram_error_hides_token(self):
        token = "123456789:AAHabcdefghijklmnopqrstuvwxyz"
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("Chat not found " + token)))
        with self.assertRaises(notify.NotifyError) as caught:
            await notify.send_text("hi", bot=bot)
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("通知发送失败", str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    async def test_admin_can_change_target_and_ping(self):
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)))
        app = create_web_app("secret", bot=bot)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.post("/settings/notify", data={"notify_chat_id": "-1001"})).status_code, 401)
            await client.post("/login", data={"password": "secret"})
            page = await client.get("/settings")
            self.assertEqual(page.status_code, 200)
            self.assertIn(notify.DEFAULT_NOTIFY_CHAT_ID, page.text)
            self.assertIn('name="notify_chat_id"', page.text)
            self.assertIn("无需重启", page.text)
            self.assertIn("测试推送", page.text)
            denied = await client.post(
                "/settings/notify",
                data={"notify_chat_id": "-1001"},
                headers={"Origin": "https://evil.test"},
            )
            self.assertEqual(denied.status_code, 403)
            bad = await client.post("/settings/notify", data={"notify_chat_id": "abc", "notify_label": "nope"})
            self.assertEqual(bad.status_code, 200)
            self.assertIn("Chat ID 必须是整数", bad.text)
            self.assertEqual(await get_bot_config(notify.NOTIFY_CHAT_ID_KEY), notify.DEFAULT_NOTIFY_CHAT_ID)
            saved = await client.post(
                "/settings/notify",
                data={"notify_chat_id": " -100123 ", "notify_label": "  运维\n频道  "},
                follow_redirects=True,
            )
            self.assertEqual(saved.status_code, 200)
            self.assertIn("通知目标已保存，立即生效，无需重启。", saved.text)
            self.assertIn('value="-100123"', saved.text)
            self.assertEqual(await get_bot_config(notify.NOTIFY_CHAT_ID_KEY), "-100123")
            self.assertEqual(await get_bot_config(notify.NOTIFY_LABEL_KEY), "运维 频道")
            self.assertEqual((await notify.get_destination()).chat_id, -100123)
            sent = await client.post("/settings/notify/test", follow_redirects=True)
            self.assertIn("测试消息已发送", sent.text)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args, (-100123, notify.TEST_PING))

    async def test_ping_without_bot_reports_the_error(self):
        app = create_web_app("secret", bot=None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/login", data={"password": "secret"})
            result = await client.post("/settings/notify/test")
        self.assertEqual(result.status_code, 200)
        self.assertIn("机器人尚未连接", result.text)


if __name__ == "__main__":
    unittest.main()
