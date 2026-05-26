"""
PlayStation game query module.

Commands (private chat + active groups):
  /psn [psn_id]          — PSN user profile (trophy level, platinum count, recent games)
  /psprice <title>       — PS Store price via PSPrices.com (CN + US)
  /trophy <psn_id> <game>— Trophy progress for a specific game

Inline:
  @bot game <title>      — RAWG.io PS4/PS5 game search
  @bot psn <psn_id>      — PSN user profile inline card

API keys stored in BotConfig:
  rawg_api_key  — RAWG.io free API key (rawg.io/apidocs)
  psn_npsso     — PSN NPSSO token (from ca.account.sony.com/api/v1/ssocookie cookie)
"""

from __future__ import annotations

import asyncio
import html
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from bot.cache import TTLCache
from bot.database import get_bot_config, get_group_settings, set_bot_config
from bot.logger import get_logger
from bot.router import registry

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

game_cache  = TTLCache(ttl=3600, max_size=500)
psn_cache   = TTLCache(ttl=300,  max_size=200)
price_cache = TTLCache(ttl=3600, max_size=300)

# ---------------------------------------------------------------------------
# RAWG.io — game search
# ---------------------------------------------------------------------------

_RAWG_BASE = "https://api.rawg.io/api"
# 18 = PS4, 187 = PS5
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


def _format_game(item: Dict) -> str:
    name = html.escape(item.get("name", "Unknown"))
    released = item.get("released", "")
    rating = item.get("rating", 0)
    metacritic = item.get("metacritic")
    genres = ", ".join(g["name"] for g in item.get("genres", [])[:4])
    platforms = item.get("platforms", [])
    ps_platforms = []
    for p in platforms:
        pid = p.get("platform", {}).get("id", 0)
        pname = _PS_PLATFORM_NAMES.get(pid)
        if pname and pname.startswith("PS"):
            ps_platforms.append(pname)

    slug = item.get("slug", "")
    store_url = f"https://store.playstation.com/search/{'+'.join(name.split())}"

    lines = [f"🎮 <b>{name}</b>"]
    if ps_platforms:
        lines.append(f"🕹 {' / '.join(ps_platforms)}")
    meta_str = f"  Metacritic: <b>{metacritic}</b>" if metacritic else ""
    if released or rating:
        rating_str = f"⭐ {rating:.1f}/5" if rating else ""
        date_str = f"📅 {released}" if released else ""
        parts = [s for s in [date_str, rating_str] if s]
        if parts or meta_str:
            lines.append("  ".join(parts) + meta_str)
    if genres:
        lines.append(f"🏷 {html.escape(genres)}")
    lines.append(f'\n🔗 <a href="{store_url}">PS Store</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PSN unofficial API — user profile & trophies
# ---------------------------------------------------------------------------

_PSN_AUTH_URL  = "https://ca.account.sony.com/api/authz/v3/oauth/token"
_PSN_TROPHY_BASE = "https://m.np.playstation.com/api/trophy/v1"
_PSN_USER_BASE   = "https://m.np.playstation.com/api/userProfile/v1"


async def _get_psn_token() -> Optional[str]:
    """Exchange NPSSO token for a short-lived PSN access token, cache result."""
    npsso = await get_bot_config("psn_npsso")
    if not npsso:
        return None

    # Check cached token
    token = await get_bot_config("psn_access_token")
    expiry_str = await get_bot_config("psn_token_expiry")
    if token and expiry_str:
        try:
            if float(expiry_str) > time.time() + 60:
                return token
        except ValueError:
            pass

    # Exchange NPSSO for access token
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _PSN_AUTH_URL,
                data={
                    "grant_type": "sso_cookie",
                    "npsso": npsso,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": f"npsso={npsso}",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    log.warning("PSN token exchange failed", status=r.status)
                    return None
                data = await r.json()
    except Exception as e:
        log.warning("PSN token request error", error=str(e))
        return None

    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 3600)
    if access_token:
        await set_bot_config("psn_access_token", access_token)
        await set_bot_config("psn_token_expiry", str(time.time() + expires_in))
    return access_token


async def _psn_get(path: str, token: str) -> Optional[Dict]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                path,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        log.warning("PSN API request failed", error=str(e))
        return None


async def get_psn_profile(psn_id: str) -> Optional[Dict[str, Any]]:
    """Fetch PSN user profile: trophy summary + recently played games."""
    cache_key = f"psn:{psn_id.lower()}"
    cached = await psn_cache.get(cache_key)
    if cached is not None:
        return cached

    token = await _get_psn_token()
    if not token:
        return None

    # Resolve account ID from online ID
    profile_url = f"{_PSN_USER_BASE}/internal-users/me/profiles"
    target_url = f"{_PSN_USER_BASE}/profiles?fields=onlineId,accountId,personalDetailSharing&onlineId={psn_id}"
    profile_data = await _psn_get(target_url, token)
    if not profile_data:
        # fallback: try to use "me" endpoint then search
        return None

    profiles = profile_data.get("profiles", [])
    if not profiles:
        return None

    account_id = profiles[0].get("accountId")
    if not account_id:
        return None

    # Trophy summary
    trophy_url = f"{_PSN_TROPHY_BASE}/users/{account_id}/trophySummary"
    recent_url = f"{_PSN_TROPHY_BASE}/users/{account_id}/recentlyPlayedTitles?limit=5"

    trophy_data, recent_data = await asyncio.gather(
        _psn_get(trophy_url, token),
        _psn_get(recent_url, token),
    )

    result = {
        "online_id": psn_id,
        "account_id": account_id,
        "trophies": trophy_data or {},
        "recent": (recent_data or {}).get("titles", []),
    }
    await psn_cache.set(cache_key, result)
    return result


def _format_psn_profile(data: Dict) -> str:
    online_id = html.escape(data.get("online_id", "?"))
    t = data.get("trophies", {})
    earned = t.get("earnedTrophies", {})
    bronze   = earned.get("bronze", 0)
    silver   = earned.get("silver", 0)
    gold     = earned.get("gold", 0)
    platinum = earned.get("platinum", 0)
    level    = t.get("trophyLevel", "?")
    progress = t.get("progress", "?")

    recent = data.get("recent", [])
    recent_names = [html.escape(g.get("trophyTitleName", "?")) for g in recent[:3]]

    lines = [
        f"🎮 <b>PSN: {online_id}</b>",
        f"🏆 🥉{bronze}  🥈{silver}  🥇{gold}  💎{platinum}",
        f"🎯 Trophy Level: <b>{level}</b>  ({progress}%)",
    ]
    if recent_names:
        lines.append(f"🕹 最近游戏：{' / '.join(recent_names)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trophy list for a game
# ---------------------------------------------------------------------------

async def get_game_trophies(account_id: str, np_comm_id: str, token: str) -> Optional[Dict]:
    url = f"{_PSN_TROPHY_BASE}/npCommunicationIds/{np_comm_id}/trophyGroups/all/trophies"
    return await _psn_get(url, token)


async def get_user_game_trophies(account_id: str, np_comm_id: str, token: str) -> Optional[Dict]:
    url = f"{_PSN_TROPHY_BASE}/users/{account_id}/npCommunicationIds/{np_comm_id}/trophyGroups/all/trophies"
    return await _psn_get(url, token)


# ---------------------------------------------------------------------------
# PS Store price — PSPrices.com
# ---------------------------------------------------------------------------

_PSPRICES_URL = "https://psprices.com/region-{region}/search/?q={query}&platform={platform}&show=games&dlc=show"
_PSPRICES_API  = "https://psprices.com/region-{region}/api/v1/games?search={query}&platform={platform}&limit=3"


async def search_psprices(query: str, regions: Tuple[str, ...] = ("us", "cn")) -> Optional[List[Dict]]:
    """Search PSPrices.com for game prices. Returns list of price entries."""
    cache_key = f"price:{query.lower()}"
    cached = await price_cache.get(cache_key)
    if cached is not None:
        return cached

    results = []
    try:
        async with aiohttp.ClientSession() as s:
            for region in regions:
                url = _PSPRICES_API.format(region=region, query=query.replace(" ", "+"), platform="PS5")
                try:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as r:
                        if r.status == 200:
                            data = await r.json()
                            games = data.get("games", data.get("data", []))
                            if games:
                                results.append({"region": region.upper(), "games": games[:2]})
                except Exception:
                    pass
    except Exception as e:
        log.warning("PSPrices request failed", error=str(e))

    if results:
        await price_cache.set(cache_key, results)
        return results
    return None


def _format_price(results: List[Dict], query: str) -> str:
    lines = [f"💰 <b>PS Store 价格</b>：{html.escape(query)}\n"]
    for region_data in results:
        region = region_data["region"]
        flag = "🇺🇸" if region == "US" else ("🇨🇳" if region == "CN" else f"[{region}]")
        for game in region_data["games"][:1]:
            name = html.escape(str(game.get("name", game.get("title", "?")))[:50])
            price = game.get("price", game.get("currentPrice", "?"))
            lowest = game.get("lowestPrice", game.get("lowestEver", {}).get("price"))
            if isinstance(price, (int, float)):
                price = f"{price/100:.2f}" if price > 100 else str(price)
            price_str = f"<b>{price}</b>" if price and price != "?" else "N/A"
            lowest_str = f"  史低: {lowest}" if lowest else ""
            lines.append(f"{flag} {name}: {price_str}{lowest_str}")

    if len(lines) == 1:
        lines.append("未找到价格信息，请访问 <a href=\"https://psprices.com\">PSPrices.com</a> 查询")

    psp_url = f"https://psprices.com/region-us/search/?q={'+'.join(query.split())}&platform=PS5&show=games"
    lines.append(f'\n🔗 <a href="{psp_url}">PSPrices</a>')
    return "\n".join(lines)


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

async def cmd_psn(update: Update, context) -> None:
    """/psn [psn_id] — query a PSN user profile."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not await _check_active(chat.id, chat.type):
        return

    args = context.args or []
    psn_id = args[0] if args else None
    if not psn_id:
        await msg.reply_text("用法：<code>/psn &lt;PSN ID&gt;</code>", parse_mode=ParseMode.HTML)
        return

    npsso = await get_bot_config("psn_npsso")
    if not npsso:
        await msg.reply_text("❌ PSN NPSSO Token 未配置，请在 Web Admin → Settings 中填写。")
        return

    status = await msg.reply_text(f"⏳ 正在查询 {html.escape(psn_id)}…")
    try:
        profile = await asyncio.wait_for(get_psn_profile(psn_id), timeout=15)
        if profile is None:
            await status.edit_text(f"❌ 未找到用户 <code>{html.escape(psn_id)}</code>，请确认 PSN ID 正确。",
                                   parse_mode=ParseMode.HTML)
            return
        text = _format_psn_profile(profile)
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    except asyncio.TimeoutError:
        await status.edit_text("❌ 查询超时，请稍后再试。")
    except Exception as e:
        log.warning("PSN profile query failed", error=str(e), psn_id=psn_id)
        await status.edit_text(f"❌ 查询失败：<code>{html.escape(str(e)[:200])}</code>",
                                parse_mode=ParseMode.HTML)


async def cmd_psprice(update: Update, context) -> None:
    """/psprice <title> — check PS Store price and history low."""
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
    try:
        results = await asyncio.wait_for(search_psprices(query), timeout=15)
        if not results:
            psp_url = f"https://psprices.com/region-us/search/?q={'+'.join(query.split())}&platform=PS5&show=games"
            await status.edit_text(
                f"❌ 未找到 <b>{html.escape(query)}</b> 的价格数据。\n"
                f'🔗 <a href="{psp_url}">在 PSPrices 手动查询</a>',
                parse_mode=ParseMode.HTML,
            )
            return
        await status.edit_text(_format_price(results, query), parse_mode=ParseMode.HTML)
    except asyncio.TimeoutError:
        await status.edit_text("❌ 查询超时，请稍后再试。")
    except Exception as e:
        log.warning("PSPrices query failed", error=str(e))
        await status.edit_text(f"❌ 查询失败：<code>{html.escape(str(e)[:200])}</code>",
                                parse_mode=ParseMode.HTML)


async def cmd_trophy(update: Update, context) -> None:
    """/trophy <psn_id> <game> — show trophy progress for a game."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not await _check_active(chat.id, chat.type):
        return

    args = context.args or []
    if len(args) < 2:
        await msg.reply_text(
            "用法：<code>/trophy &lt;PSN ID&gt; &lt;游戏名&gt;</code>\n"
            "例如：<code>/trophy YourPSNID Elden Ring</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    psn_id = args[0]
    game_query = " ".join(args[1:])

    npsso = await get_bot_config("psn_npsso")
    if not npsso:
        await msg.reply_text("❌ PSN NPSSO Token 未配置，请在 Web Admin → Settings 中填写。")
        return

    status = await msg.reply_text(f"⏳ 正在查询 {html.escape(psn_id)} 的 {html.escape(game_query)} 奖杯…")
    try:
        token = await asyncio.wait_for(_get_psn_token(), timeout=10)
        if not token:
            await status.edit_text("❌ PSN 认证失败，请检查 NPSSO Token 是否有效。")
            return

        # Resolve account ID via profile
        profile = await asyncio.wait_for(get_psn_profile(psn_id), timeout=10)
        if not profile:
            await status.edit_text(f"❌ 未找到 PSN 用户 <code>{html.escape(psn_id)}</code>。",
                                   parse_mode=ParseMode.HTML)
            return

        account_id = profile["account_id"]

        # Search user's game list for matching title
        url = f"{_PSN_TROPHY_BASE}/users/{account_id}/trophyTitles?limit=100"
        titles_data = await _psn_get(url, token)
        if not titles_data:
            await status.edit_text("❌ 无法获取游戏奖杯列表。")
            return

        titles = titles_data.get("trophyTitles", [])
        query_lower = game_query.lower()
        matched = [t for t in titles if query_lower in t.get("trophyTitleName", "").lower()]
        if not matched:
            await status.edit_text(
                f"❌ 在 <b>{html.escape(psn_id)}</b> 的游戏列表中未找到 <b>{html.escape(game_query)}</b>。",
                parse_mode=ParseMode.HTML,
            )
            return

        game = matched[0]
        np_comm_id = game.get("npCommunicationId", "")
        title_name = html.escape(game.get("trophyTitleName", game_query))
        earned = game.get("earnedTrophies", {})
        defined = game.get("definedTrophies", {})
        progress = game.get("progress", 0)

        lines = [
            f"🏆 <b>{html.escape(psn_id)}</b> — {title_name}",
            f"进度：<b>{progress}%</b>",
            f"💎 {earned.get('platinum',0)}/{defined.get('platinum',0)}  "
            f"🥇 {earned.get('gold',0)}/{defined.get('gold',0)}  "
            f"🥈 {earned.get('silver',0)}/{defined.get('silver',0)}  "
            f"🥉 {earned.get('bronze',0)}/{defined.get('bronze',0)}",
        ]

        # Fetch individual trophies if np_comm_id is available
        if np_comm_id:
            user_trophies = await get_user_game_trophies(account_id, np_comm_id, token)
            if user_trophies:
                trophies = user_trophies.get("trophies", [])
                earned_list = [t for t in trophies if t.get("earned")]
                not_earned = [t for t in trophies if not t.get("earned") and t.get("trophyType") == "platinum"]
                if not_earned:
                    lines.append(f"\n💎 白金尚未获得")
                elif any(t.get("trophyType") == "platinum" for t in earned_list):
                    lines.append(f"\n💎 白金已获得！🎉")

        await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except asyncio.TimeoutError:
        await status.edit_text("❌ 查询超时，请稍后再试。")
    except Exception as e:
        log.warning("Trophy query failed", error=str(e))
        await status.edit_text(f"❌ 查询失败：<code>{html.escape(str(e)[:200])}</code>",
                                parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

def setup(application: Application) -> None:
    cmds = [
        ("psn",      cmd_psn,      "查询 PSN 用户档案 [psn_id]",   False),
        ("psprice",  cmd_psprice,  "查询游戏 PS Store 价格",        False),
        ("trophy",   cmd_trophy,   "查询奖杯进度 <psn_id> <游戏>", False),
    ]
    for name, handler, desc, admin in cmds:
        registry.register_command(name, handler, desc, admin_only=admin)
        application.add_handler(CommandHandler(name, handler))
    log.info("psn module loaded")
