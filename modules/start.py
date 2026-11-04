"""Public private-chat onboarding and matching Telegram command menu."""
import html
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, filters
from bot.database import upsert_user
from bot.router import registry

COMMAND_TEXT = ('/movie 片名 — 搜索电影\n/tv 剧名 — 搜索剧集\n/music 歌名 — 搜索音乐\n'
                '/book 书名 — 搜索书籍\n/help — 使用说明\n/start — 欢迎与命令列表')

async def start(update, context):
    if update.effective_user:
        await upsert_user(update.effective_user)
    username = html.escape(context.bot.username or 'bot')
    await update.effective_message.reply_text(
        '👋 <b>欢迎使用 Ponygram</b>\n\n电影、剧集、音乐和书籍，一处查询。\n\n' + COMMAND_TEXT +
        f'\n\n也可在任意聊天输入：\n<code>@{username} movie 星际穿越</code>\n'
        'Inline 支持 movie、tv、music、book。\n\nRSS 仅推送到管理员配置的频道或群组，不向个人私聊发送。',
        parse_mode=ParseMode.HTML)

async def help_cmd(update, context):
    await start(update, context)

def setup(application):
    for name, fn, desc in [('start', start, '欢迎与命令列表'), ('help', help_cmd, '查看使用说明')]:
        registry.register_command(name, fn, desc)
        application.add_handler(CommandHandler(name, fn, filters=filters.ChatType.PRIVATE))
