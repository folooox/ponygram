"""PlayStation game query module.

Commands (private chat + active groups):
  /game <title>       — RAWG.io PS4/PS5 game search + PS Store HK price + local PS Plus status
  /psprice <title>    — PS Store HK price check via PSPrices.com

API keys stored in BotConfig:
  rawg_api_key    — RAWG.io free API key (rawg.io/apidocs)
  claude_api_key  — used for Chinese→English game name translation
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from typing import Dict, List, Optional
from urllib.parse import quote as urlquote

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import TTLCache
from bot.database import get_bot_config, get_group_settings, search_psn_games
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

game_cache  = TTLCache(ttl=3600, max_size=500)
price_cache = TTLCache(ttl=3600, max_size=300)

# ---------------------------------------------------------------------------
# RAWG.io — game search & detail
# ---------------------------------------------------------------------------

_RAWG_BASE = "https://api.rawg.io/api"
_PS_PLATFORM_IDS = "18,187"
_PS_PLATFORM_NAMES = {18: "PS4", 187: "PS5", 1: "Xbox", 4: "PC", 3: "iOS",
                      21: "Android", 7: "Nintendo Switch"}


async def _rawg(path: str, **params) -> Optional[Dict]:
    api_key = await get_bot_config("rawg_api_key")
    if not api_key:
        return None
    url = f"{_RAWG_BASE}{path}"
    params["key"] = api_key
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        log.warning("RAWG request failed", error=str(e))
        return None


async def search_games(query: str) -> Optional[List[Dict]]:
    """Search PS4/PS5 games on RAWG.io. Returns None if API key missing."""
    cache_key = f"game:{query.lower()}"
    cached = await game_cache.get(cache_key)
    if cached is not None:
        return cached

    data = await _rawg("/games", search=query, platforms=_PS_PLATFORM_IDS, page_size=5)
    if data is None:
        return None

    results = data.get("results", [])
    await game_cache.set(cache_key, results)
    return results


async def get_game_detail(game_id: int) -> Optional[Dict]:
    """Fetch RAWG /games/{id} — includes developers and publishers."""
    cache_key = f"game_detail:{game_id}"
    cached = await game_cache.get(cache_key)
    if cached is not None:
        return cached
    data = await _rawg(f"/games/{game_id}")
    if data:
        await game_cache.set(cache_key, data)
    return data


def _format_game(item: Dict, detail: Optional[Dict] = None) -> str:
    """Format core game card (without PS Plus status or price)."""
    name = html.escape(item.get("name", "Unknown"))
    released = item.get("released", "")
    rating = item.get("rating", 0)
    metacritic = item.get("metacritic")
    genres = ", ".join(g["name"] for g in item.get("genres", [])[:4])

    ps_platforms: list[str] = []
    for p in item.get("platforms", []):
        pid = p.get("platform", {}).get("id", 0)
        pname = _PS_PLATFORM_NAMES.get(pid)
        if pname and pname.startswith("PS"):
            ps_platforms.append(pname)
    ps_platforms = sorted(set(ps_platforms))

    vendor = ""
    if detail:
        devs = [d["name"] for d in detail.get("developers", [])[:2]]
        pubs = [p["name"] for p in detail.get("publishers", [])[:1]]
        if devs and pubs and pubs[0] not in devs:
            vendor = html.escape(" / ".join(devs) + f"  |  {pubs[0]}")
        elif devs:
            vendor = html.escape(" / ".join(devs))
        elif pubs:
            vendor = html.escape(pubs[0])

    store_url = f"https://store.playstation.com/zh-hant-hk/search/{urlquote(item.get('name', ''))}"

    lines = [f"🎮 <b>{name}</b>"]
    if ps_platforms:
        lines.append(f"🕹 {' / '.join(ps_platforms)}")
    if vendor:
        lines.append(f"🏢 {vendor}")
    if released:
        lines.append(f"📅 {released}")

    rating_parts = []
    if rating:
        rating_parts.append(f"⭐ {rating:.1f}/5")
    if metacritic:
        rating_parts.append(f"Metacritic: <b>{metacritic}</b>")
    if rating_parts:
        lines.append("  ·  ".join(rating_parts))

    if genres:
        lines.append(f"🏷 {html.escape(genres)}")

    lines.append(f'\n🔗 <a href="{store_url}">PS Store HK</a>')
    return "\n".join(lines)


def _format_psplus_status(local_games) -> str:
    """Compact single-line PS Plus status from local DB."""
    if not local_games:
        return "📚 PS Plus：未入库"
    tier_names = {1: "Essential（会免）", 2: "Extra", 3: "Premium"}
    parts = []
    for lg in local_games:
        tier_label = tier_names.get(lg.tier, f"Tier {lg.tier}")
        if lg.tier == 1:
            entry_str = lg.entry_date.strftime("%Y年%m月") if lg.entry_date else "?"
            exit_str = lg.exit_date.strftime("%Y年%m月") if lg.exit_date else None
            if exit_str:
                parts.append(f"{entry_str}加入过{tier_label}")
            else:
                parts.append(f"{entry_str}加入{tier_label}")
        else:
            status_icon = "✅ 在库" if lg.status == "active" else "❌ 已出库"
            parts.append(f"当前{tier_label} {status_icon}")
    return f"📚 PS Plus：{' | '.join(parts)}"


# ---------------------------------------------------------------------------
# Game navigation helpers
# ---------------------------------------------------------------------------

def _game_keyboard(items: list, current_idx: int, cache_key: str) -> Optional[InlineKeyboardMarkup]:
    """Row of buttons for switching between game results (excludes current)."""
    buttons = []
    for i, g in enumerate(items[:5]):
        if i == current_idx:
            continue
        name = g.get("name", "?")[:22]
        released = (g.get("released") or "")[:4]
        label = f"{i+1}. {name}（{released}）" if released else f"{i+1}. {name}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"game:{cache_key}:{i}"))
        if len(buttons) >= 3:
            break
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def on_game_button(update: Update, context) -> None:
    """Handle alternative game selection from navigation buttons."""
    q = update.callback_query
    assert q
    await q.answer()

    _, cache_key, idx_str = q.data.split(":", 2)
    idx = int(idx_str)

    items = await game_cache.get(cache_key)
    if not items or idx >= len(items):
        await q.edit_message_text("结果已过期，请重新搜索。")
        return

    item = items[idx]
    detail_coro = get_game_detail(item["id"]) if item.get("id") else asyncio.sleep(0)
    detail, local_games = await asyncio.gather(
        detail_coro,
        search_psn_games(item.get("name", "")),
        return_exceptions=True,
    )
    if isinstance(detail, Exception):
        detail = None
    if isinstance(local_games, Exception):
        local_games = []

    text = _format_game(item, detail)
    text += "\n" + _format_psplus_status(local_games)
    keyboard = _game_keyboard(items, idx, cache_key)

    if q.message and q.message.photo:
        try:
            await q.edit_message_caption(text[:1024], parse_mode=ParseMode.HTML,
                                         reply_markup=keyboard)
            return
        except Exception:
            pass
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                               disable_web_page_preview=False)


# ---------------------------------------------------------------------------
# PS Store price — PSPrices.com (HK only)
# ---------------------------------------------------------------------------

_PSPRICES_API = "https://psprices.com/region-hk/api/v1/games?q={query}&platform=PS5&limit=5"

_PSPRICES_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
}


async def search_psprices(query: str) -> Optional[List[Dict]]:
    """Search PSPrices.com for HK game prices. Returns list of game dicts or None."""
    cache_key = f"price:{query.lower()}"
    cached = await price_cache.get(cache_key)
    if cached is not None:
        return cached

    encoded = urlquote(query)
    url = _PSPRICES_API.format(query=encoded)
    headers = {**_PSPRICES_HEADERS, "Referer": f"https://psprices.com/region-hk/search/?q={encoded}"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8), headers=headers) as r:
                text = await r.text()
                if r.status != 200:
                    return None
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("PSPrices non-JSON", preview=text[:100])
                    return None
                games = data.get("games") or data.get("data") or data.get("results") or []
                if not games:
                    return None
                await price_cache.set(cache_key, games)
                return games
    except Exception as e:
        log.warning("PSPrices request failed", error=str(e))
        return None


def _format_price_line(games: List[Dict]) -> str:
    """Compact HK price line: 💰 HK$XXX  史低 HK$XX"""
    if not games:
        return ""
    game = games[0]
    price = game.get("price", game.get("currentPrice", ""))
    lowest = game.get("lowestPrice", (game.get("lowestEver") or {}).get("price"))
    if isinstance(price, (int, float)):
        price = f"{price/100:.2f}" if price > 100 else f"{price:.2f}"
    if not price or price in ("?", ""):
        return ""
    line = f"💰 HK${price}"
    if lowest:
        line += f"  史低 HK${lowest}"
    return line


# ---------------------------------------------------------------------------
# CJK detection + Claude game name translation
# ---------------------------------------------------------------------------

def _has_cjk(text: str) -> bool:
    return bool(re.search(r'[一-鿿぀-ゟ゠-ヿ가-힯]', text))


async def _translate_game_name(query: str) -> Optional[str]:
    """Use Claude to translate a CJK game name to English."""
    api_key = await get_bot_config("claude_api_key")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=30,
                messages=[{"role": "user", "content":
                    f"Reply with ONLY the official English title of this video game, nothing else: {query}"}],
            ),
            timeout=8,
        )
        name = msg.content[0].text.strip().strip('"').strip("'")
        return name if name else None
    except Exception as e:
        log.warning("Game name translation failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Active-group guard
# ---------------------------------------------------------------------------

async def _check_active(chat_id: int, chat_type: str) -> bool:
    if chat_type in ("group", "supergroup"):
        settings = await get_group_settings(chat_id)
        return settings.is_active
    return True


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------

async def cmd_game(update: Update, context) -> None:
    """/game <title> — PS4/PS5 game info with developer, ratings, price, and PS Plus status."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not await _check_active(chat.id, chat.type):
        return

    query = " ".join(context.args or "").strip()
    if not query:
        await msg.reply_text(
            "用法：<code>/game &lt;游戏名&gt;</code>\n"
            "例如：<code>/game Elden Ring</code> 或 <code>/game 黑神话悟空</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    rawg_key = await get_bot_config("rawg_api_key")
    if not rawg_key:
        await msg.reply_text(
            "❌ RAWG API Key 未配置，请在 Web Admin → Settings 中填写。\n"
            "获取地址：rawg.io/apidocs",
        )
        return

    status = await msg.reply_text(f"⏳ 正在搜索 {html.escape(query)}…")
    status_alive = True
    try:
        items = await asyncio.wait_for(search_games(query), timeout=10)

        if not items and _has_cjk(query):
            await status.edit_text("⏳ 正在翻译游戏名称…")
            en_name = await _translate_game_name(query)
            if en_name and en_name.lower() != query.lower():
                log.info("Game name translated", original=query, translated=en_name)
                items = await asyncio.wait_for(search_games(en_name), timeout=10)
                if items:
                    query = en_name

        if not items:
            await status.edit_text(
                f"❌ 未找到游戏：<b>{html.escape(query)}</b>",
                parse_mode=ParseMode.HTML,
            )
            return

        await status.edit_text("⏳ 正在获取详情…")

        short_key = hashlib.md5(f"game:{query.lower()}".encode()).hexdigest()[:16]
        await game_cache.set(short_key, items)

        top = items[0]
        game_name = top.get("name", query)

        detail_coro = get_game_detail(top["id"]) if top.get("id") else asyncio.sleep(0)
        price_coro = asyncio.wait_for(search_psprices(game_name), timeout=12)
        psplus_coro = search_psn_games(game_name)

        detail, price_res, local_games = await asyncio.gather(
            detail_coro, price_coro, psplus_coro, return_exceptions=True
        )
        if isinstance(detail, Exception):
            detail = None
        if isinstance(price_res, Exception):
            price_res = None
        if isinstance(local_games, Exception):
            local_games = []

        text = _format_game(top, detail)
        text += "\n" + _format_psplus_status(local_games)
        if isinstance(price_res, list) and price_res:
            price_line = _format_price_line(price_res)
            if price_line:
                psp_url = f"https://psprices.com/region-hk/search/?q={urlquote(game_name)}&platform=PS5&show=games"
                text += f'\n{price_line}  <a href="{psp_url}">PSPrices</a>'

        keyboard = _game_keyboard(items, 0, short_key)
        cover = top.get("background_image")

        await status.delete()
        status_alive = False
        if cover:
            try:
                await msg.reply_photo(cover, caption=text[:1024],
                                      parse_mode=ParseMode.HTML, reply_markup=keyboard)
                return
            except Exception:
                pass
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                              disable_web_page_preview=False)

    except asyncio.TimeoutError:
        if status_alive:
            await status.edit_text("❌ 查询超时，请稍后再试。")
    except Exception as e:
        log.warning("Game query failed", error=str(e))
        if status_alive:
            await status.edit_text(
                f"❌ 查询失败：<code>{html.escape(str(e)[:200])}</code>",
                parse_mode=ParseMode.HTML,
            )


async def cmd_psprice(update: Update, context) -> None:
    """/psprice <title> — check PS Store HK price and history low."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not await _check_active(chat.id, chat.type):
        return

    query = " ".join(context.args or "").strip()
    if not query:
        await msg.reply_text("用法：<code>/psprice &lt;游戏名&gt;</code>", parse_mode=ParseMode.HTML)
        return

    status = await msg.reply_text(f"⏳ 正在查询 {html.escape(query)} 的价格…")
    psp_url = f"https://psprices.com/region-hk/search/?q={urlquote(query)}&platform=PS5&show=games"
    try:
        games = await asyncio.wait_for(search_psprices(query), timeout=15)
        if not games:
            await status.edit_text(
                f"❌ 未找到 <b>{html.escape(query)}</b> 的价格数据。\n"
                f'🔗 <a href="{psp_url}">在 PSPrices 手动查询</a>',
                parse_mode=ParseMode.HTML,
            )
            return
        price_line = _format_price_line(games)
        text = f"💰 <b>PS Store HK</b>：{html.escape(query)}\n"
        text += price_line if price_line else "未找到价格信息"
        text += f'\n🔗 <a href="{psp_url}">PSPrices HK</a>'
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except asyncio.TimeoutError:
        await status.edit_text("❌ 查询超时，请稍后再试。")
    except Exception as e:
        log.warning("PSPrices query failed", error=str(e))
        await status.edit_text(f"❌ 查询失败：<code>{html.escape(str(e)[:200])}</code>",
                                parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    registry.register_command("game",    cmd_game,    "搜索 PS4/PS5 游戏信息")
    registry.register_command("psprice", cmd_psprice, "查询游戏 PS Store HK 价格")
    application.add_handler(CommandHandler("game",    cmd_game))
    application.add_handler(CommandHandler("psprice", cmd_psprice))
    application.add_handler(CallbackQueryHandler(on_game_button, pattern=r"^game:"))
    log.info("psn module loaded")
