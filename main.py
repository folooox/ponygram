"""
Ponygram Bot — entry point.

Usage:
    python main.py

Reads configuration from .env (copy .env.example → .env and fill in BOT_TOKEN).
Supports both long-polling (default) and webhook mode (set WEBHOOK_URL in .env).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from telegram import Bot
from telegram.ext import Application, ApplicationBuilder

from bot.config import load_config
from bot.database import init_db
from bot.error_handler import error_handler
from bot.logger import get_logger, setup_logging
from bot.plugin_loader import PluginLoader
from bot.router import registry, setup_handlers

BASE_DIR = Path(__file__).parent


def _print_banner(bot_info) -> None:
    print(
        f"\n"
        f"  ╔══════════════════════════════════╗\n"
        f"  ║         PONYGRAM BOT             ║\n"
        f"  ╠══════════════════════════════════╣\n"
        f"  ║  Name : {bot_info.first_name:<24}║\n"
        f"  ║  @{bot_info.username:<30}  ║\n"
        f"  ║  ID   : {bot_info.id:<24}║\n"
        f"  ╚══════════════════════════════════╝\n"
    )


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level, cfg.log_file)
    log = get_logger(__name__)

    log.info("Starting ponygram", log_level=cfg.log_level, webhook=cfg.use_webhook)

    # Initialise database
    await init_db(cfg.database_url)

    app: Application = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .build()
    )

    # Make config and runtime settings accessible to handlers via bot_data
    app.bot_data["config"] = cfg
    app.bot_data["rss_interval"] = int(os.environ.get("RSS_INTERVAL", "15"))

    # Register global error handler
    app.add_error_handler(error_handler)

    # Load built-in modules and optional plugins
    loader = PluginLoader()
    await loader.load_modules(app, str(BASE_DIR / "modules"))
    await loader.load_plugins(app, str(BASE_DIR / "plugins"))

    # Register any remaining handlers accumulated in the registry
    # (modules may have already added their own handlers directly)
    setup_handlers(app)

    # Start web admin UI (optional)
    _web_server = None
    if cfg.web_enabled:
        if not cfg.web_secret:
            log.warning("WEB_ENABLED=true but WEB_SECRET is not set — web UI disabled")
        else:
            try:
                import uvicorn
                from web.app import create_web_app
                web_app = create_web_app(secret=cfg.web_secret, bot=app.bot)
                uv_config = uvicorn.Config(
                    web_app,
                    host=cfg.web_host,
                    port=cfg.web_port,
                    log_level="warning",
                    access_log=False,
                )
                _web_server = uvicorn.Server(uv_config)
                asyncio.create_task(_web_server.serve())
                log.info("Web admin UI started", host=cfg.web_host, port=cfg.web_port)
            except ImportError:
                log.warning("uvicorn not installed — web UI disabled. Run: pip install uvicorn[standard]")

    # Fetch bot info and print startup banner
    async with app:
        bot_info = await app.bot.get_me()
        _print_banner(bot_info)

        # Publish public commands to Telegram (visible in command menu)
        public_commands = registry.get_bot_commands(is_admin=False)
        if public_commands:
            await app.bot.set_my_commands(public_commands)
            log.info("Bot commands set", count=len(public_commands))

        if cfg.use_webhook:
            log.info(
                "Starting webhook",
                url=cfg.webhook_url,
                port=cfg.webhook_port,
            )
            await app.updater.start_webhook(
                listen="0.0.0.0",
                port=cfg.webhook_port,
                webhook_url=cfg.webhook_url,
            )
        else:
            log.info("Starting long-polling")
            await app.updater.start_polling(drop_pending_updates=True)

        await app.start()

        log.info("Bot is running. Press Ctrl+C to stop.")

        # Block until a stop signal is received
        stop_event = asyncio.Event()

        def _stop(*_):
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_event_loop().add_signal_handler(sig, _stop)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, _stop)

        await stop_event.wait()

        log.info("Shutdown signal received, stopping…")
        await app.updater.stop()
        await app.stop()

    log.info("Ponygram stopped. Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
