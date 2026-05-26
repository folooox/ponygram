"""
Cookie management commands — owner only.

/setcookie <platform> <cookie_string>   Save or update a platform cookie
/delcookie <platform>                   Delete a platform cookie
/listcookies                            Show which platforms have cookies set
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.database import delete_bot_config, get_all_bot_configs, set_bot_config
from bot.logger import get_logger
from bot.permissions import owner_only
from bot.utils import normalize_cookie

log = get_logger(__name__)

_PLATFORM_MAP: dict[str, str] = {
    "instagram":    "cookie_instagram",
    "twitter":      "cookie_twitter",
    "x":            "cookie_twitter",
    "bilibili":     "cookie_bilibili",
    "douyin":       "cookie_douyin",
    "抖音":          "cookie_douyin",
    "tiktok":       "cookie_tiktok",
    "kuaishou":     "cookie_kuaishou",
    "快手":          "cookie_kuaishou",
    "xiaohongshu":  "cookie_xiaohongshu",
    "xhs":          "cookie_xiaohongshu",
    "小红书":        "cookie_xiaohongshu",
    "youtube":      "cookie_youtube",
}

_DISPLAY: dict[str, str] = {
    "cookie_instagram":   "Instagram",
    "cookie_twitter":     "Twitter/X",
    "cookie_bilibili":    "Bilibili",
    "cookie_douyin":      "抖音",
    "cookie_tiktok":      "TikTok",
    "cookie_kuaishou":    "快手",
    "cookie_xiaohongshu": "小红书",
    "cookie_youtube":     "YouTube",
}


@owner_only
async def cmd_setcookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /setcookie <platform> <cookie_string>"""
    msg = update.effective_message
    args = context.args or []
    if len(args) < 2:
        await msg.reply_text(
            "用法：<code>/setcookie &lt;平台&gt; &lt;cookie字符串&gt;</code>\n\n"
            "支持的平台：<code>instagram twitter bilibili douyin tiktok "
            "kuaishou xiaohongshu youtube</code>",
            parse_mode="HTML",
        )
        return

    platform = args[0].lower()
    cookie_str = " ".join(args[1:]).strip()

    db_key = _PLATFORM_MAP.get(platform)
    if not db_key:
        await msg.reply_text(
            f"❌ 未知平台：<code>{platform}</code>\n\n"
            "支持：instagram twitter bilibili douyin tiktok kuaishou xiaohongshu youtube",
            parse_mode="HTML",
        )
        return

    normalized, count = normalize_cookie(cookie_str)
    if not normalized:
        await msg.reply_text("❌ 无法识别 Cookie 格式，请使用 Header String / JSON / Netscape 格式。")
        return
    await set_bot_config(db_key, normalized)
    display = _DISPLAY.get(db_key, platform)
    log.info("Cookie updated", platform=display, cookie_count=count)
    fmt = "Header String" if ";" in cookie_str and "[" not in cookie_str else ("JSON" if cookie_str.startswith("[") else "Netscape")
    await msg.reply_text(f"✅ {display} Cookie 已更新\n识别格式：{fmt}\n共 {count} 条 Cookie")


@owner_only
async def cmd_delcookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /delcookie <platform>"""
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text("用法：<code>/delcookie &lt;平台&gt;</code>", parse_mode="HTML")
        return

    platform = args[0].lower()
    db_key = _PLATFORM_MAP.get(platform)
    if not db_key:
        await msg.reply_text(f"❌ 未知平台：{platform}", parse_mode="HTML")
        return

    await delete_bot_config(db_key)
    display = _DISPLAY.get(db_key, platform)
    await msg.reply_text(f"🗑 {display} Cookie 已删除")


@owner_only
async def cmd_listcookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all configured platform cookies with health status."""
    msg = update.effective_message
    configs = await get_all_bot_configs()
    lines = ["<b>平台 Cookie 状态</b>\n"]
    for db_key, display in _DISPLAY.items():
        val = configs.get(db_key)
        if val:
            status = configs.get(f"status_{db_key}", "")
            if status == "valid":
                icon = "🟢"
            elif status == "expired":
                icon = "🔴"
            else:
                icon = "✅"
            preview = val[:20] + "…" if len(val) > 20 else val
            lines.append(f"{icon} <b>{display}</b>  <code>{preview}</code>")
        else:
            lines.append(f"⬜ <b>{display}</b>  未配置")
    lines.append("\n🟢=已验证有效  🔴=已检测失效  ✅=配置但未检测")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


def setup(application: Application) -> None:
    application.add_handler(CommandHandler("setcookie", cmd_setcookie))
    application.add_handler(CommandHandler("delcookie", cmd_delcookie))
    application.add_handler(CommandHandler("listcookies", cmd_listcookies))
    log.info("cookies module loaded")
