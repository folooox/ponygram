"""PlayStation game query module.

Commands (private chat + active groups):
  /game <title>       — RAWG.io PS4/PS5 game search + PS Store HK price + local PS Plus status
  /psprice <title>    — PS Store HK price check via PSPrices.com

API keys stored in BotConfig:
  rawg_api_key       — RAWG.io free API key (rawg.io/apidocs)
  deepseek_api_key   — DeepSeek API key for CJK→English translation (primary)
  claude_api_key     — fallback for CJK→English translation
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote as urlquote

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.cache import TTLCache
from bot.database import get_bot_config, get_group_settings, match_psn_game, search_psn_games
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

game_cache      = TTLCache(ttl=3600,  max_size=500)
price_cache     = TTLCache(ttl=3600,  max_size=300)
translate_cache = TTLCache(ttl=86400, max_size=2000)

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
    """Fetch RAWG /games/{id} — includes developers, publishers, and stores."""
    cache_key = f"game_detail:{game_id}"
    cached = await game_cache.get(cache_key)
    if cached is not None:
        return cached
    data = await _rawg(f"/games/{game_id}")
    if data:
        await game_cache.set(cache_key, data)
    return data


def _get_ps_store_url(detail: Optional[Dict], game_name: str, db_game=None) -> str:
    """Extract PS Store URL: DB store_url → DB product_id → RAWG stores → HK search."""
    if db_game:
        if db_game.store_url:
            return db_game.store_url
        if db_game.product_id:
            return f"https://store.playstation.com/zh-hant-hk/product/{db_game.product_id}"
    if detail:
        for store in detail.get("stores", []):
            slug = (store.get("store") or {}).get("slug", "")
            if slug == "playstation-store":
                url = store.get("url", "")
                if url:
                    return url
    return f"https://store.playstation.com/zh-hant-hk/search/{urlquote(game_name)}"


def _norm_vendor(s: str) -> str:
    return re.sub(r'[\s\W_]+', '', s).lower()


def _dedup_vendors(names: list) -> list:
    """Remove case-insensitive substring-duplicate vendor names."""
    out: list = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        norm = _norm_vendor(n)
        if any(norm in _norm_vendor(o) or _norm_vendor(o) in norm for o in out):
            continue
        out.append(n)
    return out


def _format_game(item: Dict, detail: Optional[Dict] = None,
                 store_url: Optional[str] = None,
                 title_zh: Optional[str] = None,
                 developer_zh: Optional[str] = None) -> str:
    """Format core game card (without PS Plus status or price)."""
    name_en = html.escape(item.get("name", "Unknown"))
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

    vendor_en = ""
    if detail:
        devs = _dedup_vendors([d["name"] for d in detail.get("developers", [])[:3]])
        pubs = _dedup_vendors([p["name"] for p in detail.get("publishers", [])[:2]])
        # Drop publishers that are substrings of any developer (same company, different case)
        pubs = [p for p in pubs
                if not any(_norm_vendor(p) in _norm_vendor(d) or
                           _norm_vendor(d) in _norm_vendor(p) for d in devs)]
        if devs and pubs:
            vendor_en = devs[0] + "  |  " + pubs[0]
        elif devs:
            vendor_en = " / ".join(devs[:2])
        elif pubs:
            vendor_en = pubs[0]

    if store_url is None:
        store_url = _get_ps_store_url(detail, item.get("name", ""))

    # Bilingual name: "中文名 ( English Name )" or just English
    if title_zh and title_zh.strip() and title_zh.strip() != name_en:
        name_line = f"🎮 <b>{html.escape(title_zh.strip())} ( {name_en} )</b>"
    else:
        name_line = f"🎮 <b>{name_en}</b>"

    lines = [name_line]
    if ps_platforms:
        lines.append(f"🕹 {' / '.join(ps_platforms)}")

    # Bilingual developer: only wrap in bilingual if zh is genuinely different from en
    dev_en_base = vendor_en.split("  |  ")[0] if "  |  " in vendor_en else vendor_en
    if (developer_zh and vendor_en
            and _norm_vendor(developer_zh) != _norm_vendor(dev_en_base)):
        lines.append(f"🏢 {html.escape(developer_zh.strip())} ( {html.escape(vendor_en)} )")
    elif vendor_en:
        lines.append(f"🏢 {html.escape(vendor_en)}")

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
# CJK detection + game name translation (DeepSeek → Claude fallback)
# ---------------------------------------------------------------------------

def _has_cjk(text: str) -> bool:
    return bool(re.search(r'[一-鿿぀-ゟ゠-ヿ가-힯]', text))


async def _translate_game_name(query: str) -> Optional[str]:
    """Translate CJK game name to English. Tries DeepSeek first, falls back to Claude."""
    # Try DeepSeek (faster, cheaper)
    deepseek_key = await get_bot_config("deepseek_api_key")
    if deepseek_key:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {deepseek_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content":
                            f"Reply with ONLY the official English title of this video game, "
                            f"nothing else: {query}"}],
                        "max_tokens": 30,
                        "temperature": 0,
                    },
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        name = (data["choices"][0]["message"]["content"]
                                .strip().strip('"').strip("'"))
                        if name:
                            return name
        except Exception as e:
            log.warning("DeepSeek translation failed", error=str(e))

    # Fall back to Claude Haiku
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
                    f"Reply with ONLY the official English title of this video game, "
                    f"nothing else: {query}"}],
            ),
            timeout=8,
        )
        name = msg.content[0].text.strip().strip('"').strip("'")
        return name if name else None
    except Exception as e:
        log.warning("Game name translation (Claude) failed", error=str(e))
        return None


async def _translate_game_data_to_zh(en_name: str, en_developer: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Translate English game name + developer to Chinese in one AI call. Cached 24h."""
    cache_key = f"zh:{en_name}|{en_developer}"
    cached = await translate_cache.get(cache_key)
    if cached is not None:
        return cached

    prompt = (
        f'Translate this video game info to Simplified Chinese. '
        f'Reply with ONLY a JSON object: {{"name_zh": "...", "dev_zh": "..."}}. '
        f'If already Chinese or unknown, keep the original. '
        f'Game: "{en_name}", Developer: "{en_developer}"'
    )

    result: Tuple[Optional[str], Optional[str]] = (None, None)

    deepseek_key = await get_bot_config("deepseek_api_key")
    if deepseek_key:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {deepseek_key}",
                             "Content-Type": "application/json"},
                    json={"model": "deepseek-chat",
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 80, "temperature": 0},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        text = data["choices"][0]["message"]["content"].strip()
                        # Extract JSON from possible code blocks
                        text = re.sub(r"```[a-z]*\n?", "", text).strip("` \n")
                        parsed = json.loads(text)
                        result = (parsed.get("name_zh") or None, parsed.get("dev_zh") or None)
        except Exception as e:
            log.warning("DeepSeek zh translation failed", error=str(e))

    if result == (None, None):
        api_key = await get_bot_config("claude_api_key")
        if api_key:
            try:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=api_key)
                msg = await asyncio.wait_for(
                    client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=80,
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=8,
                )
                text = msg.content[0].text.strip()
                text = re.sub(r"```[a-z]*\n?", "", text).strip("` \n")
                parsed = json.loads(text)
                result = (parsed.get("name_zh") or None, parsed.get("dev_zh") or None)
            except Exception as e:
                log.warning("Claude zh translation failed", error=str(e))

    # If dev_zh is essentially the same as en_developer (just transliteration/case),
    # drop it to avoid wrapping foreign names in fake bilingual brackets.
    name_zh_out, dev_zh_out = result
    if dev_zh_out and en_developer:
        if _norm_vendor(dev_zh_out) == _norm_vendor(en_developer):
            dev_zh_out = None
    result = (name_zh_out, dev_zh_out)

    await translate_cache.set(cache_key, result)
    return result


async def _smart_search(query: str) -> Tuple[Optional[List[Dict]], str]:
    """For CJK queries, translate to English first; returns (items, final_query)."""
    if _has_cjk(query):
        en_name = await _translate_game_name(query)
        if en_name and en_name.lower() != query.lower():
            log.info("Game name translated", original=query, translated=en_name)
            items = await asyncio.wait_for(search_games(en_name), timeout=10)
            if items:
                return items, en_name
    items = await asyncio.wait_for(search_games(query), timeout=10)
    return items, query


# ---------------------------------------------------------------------------
# Selection-first keyboard + callback
# ---------------------------------------------------------------------------

def _game_select_keyboard(items: list, cache_key: str) -> InlineKeyboardMarkup:
    """2-per-row selection buttons for up to 5 game results."""
    rows: list = []
    row: list = []
    for i, g in enumerate(items[:5]):
        name = g.get("name", "?")
        released = (g.get("released") or "")[:4]
        label = (f"{i+1}. {name[:15]}（{released}）" if released
                 else f"{i+1}. {name[:22]}")
        row.append(InlineKeyboardButton(label, callback_data=f"game_sel:{cache_key}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def on_game_select(update: Update, context) -> None:
    """Handle game selection — fetches full detail and sends photo card."""
    q = update.callback_query
    assert q
    await q.answer()

    _, cache_key, idx_str = q.data.split(":", 2)
    idx = int(idx_str)

    items = await game_cache.get(cache_key)
    if not items or idx >= len(items):
        await q.edit_message_text("结果已过期，请重新搜索。")
        return

    await q.edit_message_text("⏳ 正在获取详情…", reply_markup=None)

    item = items[idx]
    game_name = item.get("name", "")

    detail_coro  = get_game_detail(item["id"]) if item.get("id") else asyncio.sleep(0)
    price_coro   = asyncio.wait_for(search_psprices(game_name), timeout=12)
    psplus_coro  = search_psn_games(game_name)
    dbmatch_coro = match_psn_game(game_name)

    detail, price_res, local_games, db_game = await asyncio.gather(
        detail_coro, price_coro, psplus_coro, dbmatch_coro, return_exceptions=True
    )
    if isinstance(detail, Exception):     detail = None
    if isinstance(price_res, Exception):  price_res = None
    if isinstance(local_games, Exception): local_games = []
    if isinstance(db_game, Exception):    db_game = None

    developer_en = ""
    if detail:
        devs = [d["name"] for d in detail.get("developers", [])[:1]]
        developer_en = devs[0] if devs else ""

    title_zh, developer_zh = await _translate_game_data_to_zh(game_name, developer_en)
    if db_game and db_game.title_zh:
        title_zh = db_game.title_zh

    store_url = _get_ps_store_url(detail, game_name, db_game=db_game)
    text = _format_game(item, detail, store_url=store_url,
                        title_zh=title_zh, developer_zh=developer_zh)
    text += "\n" + _format_psplus_status(local_games)
    if isinstance(price_res, list) and price_res:
        price_line = _format_price_line(price_res)
        if price_line:
            psp_url = (f"https://psprices.com/region-hk/search/"
                       f"?q={urlquote(game_name)}&platform=PS5&show=games")
            text += f'\n{price_line}  <a href="{psp_url}">PSPrices</a>'

    cover    = item.get("background_image")
    chat_id  = q.message.chat_id if q.message else None

    try:
        await q.message.delete()
    except Exception:
        pass

    if cover and chat_id:
        try:
            await context.bot.send_photo(
                chat_id, cover,
                caption=text[:1024],
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass
    if chat_id:
        await context.bot.send_message(
            chat_id, text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )


# ---------------------------------------------------------------------------
# PS Store price — PSPrices.com (HK only)
# ---------------------------------------------------------------------------

_PSPRICES_API = "https://psprices.com/region-hk/api/v1/games?q={query}&platform=PS5&limit=5"
_PSPRICES_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
}


async def search_psprices(query: str) -> Optional[List[Dict]]:
    """Search PSPrices.com for HK game prices."""
    cache_key = f"price:{query.lower()}"
    cached = await price_cache.get(cache_key)
    if cached is not None:
        return cached

    encoded = urlquote(query)
    url = _PSPRICES_API.format(query=encoded)
    headers = {**_PSPRICES_HEADERS,
               "Referer": f"https://psprices.com/region-hk/search/?q={encoded}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8),
                             headers=headers) as r:
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
    price  = game.get("price", game.get("currentPrice", ""))
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
    """/game <title> — show selection list, then full card on pick."""
    msg  = update.effective_message
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

    if not await get_bot_config("rawg_api_key"):
        await msg.reply_text(
            "❌ RAWG API Key 未配置，请在 Web Admin → Settings 中填写。\n"
            "获取地址：rawg.io/apidocs",
        )
        return

    status = await msg.reply_text(f"⏳ 正在搜索 {html.escape(query)}…")
    status_alive = True
    try:
        items, final_query = await _smart_search(query)

        if not items:
            await status.edit_text(
                f"❌ 未找到游戏：<b>{html.escape(query)}</b>",
                parse_mode=ParseMode.HTML,
            )
            return

        short_key = hashlib.md5(f"game:{final_query.lower()}".encode()).hexdigest()[:16]
        await game_cache.set(short_key, items)

        if len(items) == 1:
            # Single result — show card immediately
            top       = items[0]
            game_name = top.get("name", final_query)

            await status.edit_text("⏳ 正在获取详情…")

            detail_coro  = get_game_detail(top["id"]) if top.get("id") else asyncio.sleep(0)
            detail, price_res, local_games, db_game = await asyncio.gather(
                detail_coro,
                asyncio.wait_for(search_psprices(game_name), timeout=12),
                search_psn_games(game_name),
                match_psn_game(game_name, raw_query=query),
                return_exceptions=True,
            )
            if isinstance(detail, Exception):     detail = None
            if isinstance(price_res, Exception):  price_res = None
            if isinstance(local_games, Exception): local_games = []
            if isinstance(db_game, Exception):    db_game = None

            developer_en = ""
            if detail:
                devs = [d["name"] for d in detail.get("developers", [])[:1]]
                developer_en = devs[0] if devs else ""

            title_zh, developer_zh = await _translate_game_data_to_zh(game_name, developer_en)
            if db_game and db_game.title_zh:
                title_zh = db_game.title_zh

            store_url = _get_ps_store_url(detail, game_name, db_game=db_game)
            text = _format_game(top, detail, store_url=store_url,
                                title_zh=title_zh, developer_zh=developer_zh)
            text += "\n" + _format_psplus_status(local_games)
            if isinstance(price_res, list) and price_res:
                price_line = _format_price_line(price_res)
                if price_line:
                    psp_url = (f"https://psprices.com/region-hk/search/"
                               f"?q={urlquote(game_name)}&platform=PS5&show=games")
                    text += f'\n{price_line}  <a href="{psp_url}">PSPrices</a>'

            cover = top.get("background_image")
            await status.delete()
            status_alive = False
            if cover:
                try:
                    await msg.reply_photo(cover, caption=text[:1024],
                                          parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    pass
            await msg.reply_text(text, parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=False)

        else:
            # Multiple results — selection list
            lines = [f"🎮 搜到 <b>{len(items)}</b> 个结果，请选择：\n"]
            for i, g in enumerate(items[:5]):
                name     = g.get("name", "?")
                released = (g.get("released") or "")[:4]
                ps_plats = sorted(set(
                    _PS_PLATFORM_NAMES[p.get("platform", {}).get("id", 0)]
                    for p in g.get("platforms", [])
                    if _PS_PLATFORM_NAMES.get(
                        p.get("platform", {}).get("id", 0), ""
                    ).startswith("PS")
                ))
                rating = g.get("rating", 0)
                line = f"{i+1}. <b>{html.escape(name)}</b>"
                if released:   line += f" ({released})"
                if ps_plats:   line += f" · {' / '.join(ps_plats)}"
                if rating:     line += f" · ⭐{rating:.1f}"
                lines.append(line)

            keyboard = _game_select_keyboard(items, short_key)
            await status.edit_text(
                "\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            status_alive = False

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
    msg  = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not await _check_active(chat.id, chat.type):
        return

    query = " ".join(context.args or "").strip()
    if not query:
        await msg.reply_text("用法：<code>/psprice &lt;游戏名&gt;</code>",
                             parse_mode=ParseMode.HTML)
        return

    status  = await msg.reply_text(f"⏳ 正在查询 {html.escape(query)} 的价格…")
    psp_url = (f"https://psprices.com/region-hk/search/"
               f"?q={urlquote(query)}&platform=PS5&show=games")
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
        text  = f"💰 <b>PS Store HK</b>：{html.escape(query)}\n"
        text += price_line if price_line else "未找到价格信息"
        text += f'\n🔗 <a href="{psp_url}">PSPrices HK</a>'
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except asyncio.TimeoutError:
        await status.edit_text("❌ 查询超时，请稍后再试。")
    except Exception as e:
        log.warning("PSPrices query failed", error=str(e))
        await status.edit_text(
            f"❌ 查询失败：<code>{html.escape(str(e)[:200])}</code>",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    registry.register_command("game",    cmd_game,    "搜索 PS4/PS5 游戏信息")
    registry.register_command("psprice", cmd_psprice, "查询游戏 PS Store HK 价格")
    application.add_handler(CommandHandler("game",    cmd_game))
    application.add_handler(CommandHandler("psprice", cmd_psprice))
    application.add_handler(CallbackQueryHandler(on_game_select, pattern=r"^game_sel:"))
    log.info("psn module loaded")
