"""Thin Telegram adapters for the shared four-category catalog."""
import asyncio
import hashlib
import html
import secrets
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle,
                      InputTextMessageContent, LinkPreviewOptions)
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, InlineQueryHandler, filters
from bot.cache import TTLCache
from bot.router import registry
from bot.database import upsert_user
from services import catalog

selections = TTLCache(ttl=900, max_size=1000)


def card_keyboard(card):
    links = [(card.source, card.url)] + card.extra_links
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)] for label, url in links if catalog.safe_url(url)])


async def send_card(message, card):
    text = catalog.render_card(card)
    keyboard = card_keyboard(card)
    if card.image:
        try:
            await message.reply_photo(card.image, caption=text if len(text) <= 1000 else None,
                                      parse_mode=ParseMode.HTML, reply_markup=keyboard if len(text) <= 1000 else None)
            if len(text) <= 1000:
                return
        except Exception:
            pass  # bad/missing cover must not prevent the actual detail card
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                             link_preview_options=LinkPreviewOptions(is_disabled=True))


async def command(update, context):
    if getattr(update, "effective_user", None):
        await upsert_user(update.effective_user, source="private")
    msg = update.effective_message
    kind = msg.text.split()[0].split('@')[0].lstrip('/').lower()
    if not context.args:
        await msg.reply_text(f'用法：/{kind} <{catalog.KINDS[kind]}名或关键词>')
        return
    status = await msg.reply_text('🔍 正在搜索…')
    try:
        results = await asyncio.wait_for(catalog.search(kind, ' '.join(context.args)), timeout=12)
        if not results:
            await status.edit_text('没有找到匹配结果，请换一个关键词。')
            return
        token = secrets.token_hex(8)
        await selections.set(token, (update.effective_user.id, results))
        rows = [[InlineKeyboardButton(f'{i+1}. {c.title[:55]}', callback_data=f'catalog:{token}:{i}')]
                for i, c in enumerate(results)]
        await status.edit_text(f'{catalog.ICONS[kind]} 找到 {len(results)} 个结果，点击查看详情：',
                               reply_markup=InlineKeyboardMarkup(rows))
    except (catalog.CatalogError, TimeoutError) as e:
        await status.edit_text(str(e) if isinstance(e, catalog.CatalogError) else '搜索超时，请稍后重试。')


async def selected(update, context):
    q = update.callback_query
    try:
        _, token, index = q.data.split(':')
        stored = await selections.get(token)
        if not stored:
            await q.answer('结果已过期，请重新搜索。', show_alert=True)
            return
        owner, results = stored
        if q.from_user.id != owner:
            await q.answer('请使用自己的搜索结果。', show_alert=True)
            return
        if not 0 <= int(index) < len(results):
            await q.answer('无效的结果。', show_alert=True)
            return
        await upsert_user(q.from_user, source='private')
        card = results[int(index)]
        await q.answer('正在加载详情…')
        if not await catalog.enabled(card.kind):
            await q.message.reply_text('该查询功能暂时停用。')
            return
        card = await asyncio.wait_for(catalog.detail(card), timeout=16)
        await send_card(q.message, card)
    except (ValueError, TimeoutError, catalog.CatalogError):
        if q.message:
            await q.message.reply_text('加载失败，请稍后重试。')


def help_result(message):
    return InlineQueryResultArticle(id='help', title='Ponygram 查询指南', description='movie / tv / music / book ＋ 关键词',
        input_message_content=InputTextMessageContent(message, parse_mode=ParseMode.HTML))


async def inline(update, context):
    if getattr(update, "effective_user", None):
        await upsert_user(update.effective_user, source="inline")
    query = update.inline_query
    parts = (query.query or '').strip().split(None, 1)
    if len(parts) < 2 or parts[0].lower() not in catalog.KINDS:
        await query.answer([help_result('使用 <b>movie / tv / music / book</b> ＋ 关键词查询。')], cache_time=30)
        return
    kind, word = parts[0].lower(), parts[1]
    try:
        results = await asyncio.wait_for(catalog.search(kind, word), timeout=12)
        details = await asyncio.wait_for(asyncio.gather(*[catalog.detail(c) for c in results]), timeout=16)
        items = []
        for card in details:
            preview = LinkPreviewOptions(url=card.image, prefer_large_media=True, show_above_text=True) if card.image else LinkPreviewOptions(is_disabled=True)
            items.append(InlineQueryResultArticle(
                id=hashlib.sha256(f'{kind}:{card.id}'.encode()).hexdigest()[:32], title=card.title,
                description=' · '.join(v for _, v in card.facts if v)[:180] or card.source,
                thumbnail_url=card.image or None,
                input_message_content=InputTextMessageContent(catalog.render_card(card), parse_mode=ParseMode.HTML,
                                                              link_preview_options=preview),
                reply_markup=card_keyboard(card)))
        await query.answer(items or [help_result('没有找到匹配结果，请换一个关键词。')], cache_time=60)
    except (catalog.CatalogError, TimeoutError) as e:
        await query.answer([help_result(html.escape(str(e)) if isinstance(e, catalog.CatalogError) else '查询超时，请稍后重试。')], cache_time=5)


def setup(application):
    for kind, label in catalog.KINDS.items():
        registry.register_command(kind, command, f'搜索{label}')
        application.add_handler(CommandHandler(kind, command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(selected, pattern=r'^catalog:'))
    application.add_handler(InlineQueryHandler(inline))
