# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Ponygram is a modular, async Telegram bot (Python 3.12+) with group management, AI dialogue (Claude), media parsing, RSS, and PlayStation game queries. It ships a FastAPI-based Web Admin UI that runs alongside the bot as an asyncio task.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — minimum required: BOT_TOKEN, OWNER_ID

# Run
python main.py
```

With Docker:
```bash
docker compose up -d
docker compose logs -f
```

There is no test suite. Manual testing requires a real Telegram bot token.

## Configuration — Two Layers

**Layer 1 — `.env` (bot identity & infrastructure):** `BOT_TOKEN`, `OWNER_ID`, `ADMIN_IDS`, `DATABASE_URL`, `LOG_LEVEL`, `WEBHOOK_URL`, `WEB_ENABLED`, `WEB_SECRET`, `WEB_PORT`. Requires restart to change.

**Layer 2 — `bot_config` DB table (API keys & secrets):** Claude API key, TMDB, Last.fm, RAWG, PSN NPSSO token, and all platform cookies are stored in the database, managed via **Web Admin UI → Settings** — no restart needed. Modules read these keys at runtime via `await get_bot_config("key_name")`.

## Architecture

### Startup Flow (`main.py`)

1. `load_config()` — reads `.env` → `Config` dataclass
2. `init_db()` — creates SQLite tables and runs `_migrate()` for column additions
3. Build `telegram.ext.Application`, put `Config` into `app.bot_data["config"]`
4. `PluginLoader.load_modules(app, "modules/")` — imports each `.py`, calls its `setup(app)`
5. `PluginLoader.load_plugins(app, "plugins/")` — same for user plugins
6. Optionally start `uvicorn` running the FastAPI web app as an asyncio task
7. Start long-polling or webhook

### Core Packages

- **`bot/config.py`** — `Config` dataclass + `load_config()`. The `Config` object is available everywhere via `context.bot_data["config"]`.
- **`bot/database.py`** — SQLAlchemy 2 async ORM over SQLite/aiosqlite. Single `get_session()` factory returns an `AsyncSession` context manager. Schema migrations are applied in `_migrate()` at startup using `ALTER TABLE` — add new columns there.
- **`bot/router.py`** — `CommandRegistry` singleton (`registry`). Modules call `registry.register_command(name, handler, description, admin_only=False)` to publish commands to `/help` and Telegram's command menu.
- **`bot/permissions.py`** — Decorators: `@admin_only` (Telegram chat admin OR `ADMIN_IDS`), `@owner_only`, `@group_only`, `@not_blacklisted`.
- **`bot/cache.py`** — `TTLCache` (async-safe, in-memory). Module-level instances: `movie_cache` (1h), `book_cache` (1h), `music_cache` (30m).
- **`bot/plugin_loader.py`** — Loads all `*.py` files from a directory alphabetically, calling `setup(app)` on each. Supports both sync and async `setup`.

### Module Convention

Every file in `modules/` and `plugins/` must expose:

```python
def setup(application: Application) -> None:   # or async def setup(...)
    application.add_handler(...)
    registry.register_command("cmd", handler, "Description")
```

Message handler priority is controlled via `group=N` in `add_handler()`. Current ordering: antispam (`group=10`) runs before ai_chat (`group=30`).

### Web Admin (`web/app.py`)

Single FastAPI `create_web_app(secret, bot)` factory. Auth is an HMAC-SHA256 signed session cookie (`WEB_SECRET` as the key). All routes check `_authed(session)` before proceeding. `/health/check/{service}` makes live API calls to verify keys; `/health/check/media` tests parsehub URL parsing. The app runs co-operatively on the same event loop as the Telegram bot.

### AI Chat (`modules/ai_chat.py`)

Uses `claude-opus-4-7` with `thinking={"type": "adaptive"}` and streaming. Per-`(user_id, chat_id)` conversation history is kept in-memory (max 20 turns). Triggers: @mention, `pony ` prefix, or reply to a bot message. Only fires in active groups where `aichat_enabled=True`; always fires in private chats.

### Media Parsing (`modules/media.py`)

URL detection via regex on every incoming message. Pre-filters against `_KNOWN_DOMAINS` before calling `parsehub`. Platform cookies (stored in `bot_config`) are injected per-domain. `DL_MAX_MB` env var caps file size sent to Telegram (default 49 MB).

### Database Schema

Key tables: `users`, `group_settings` (per-group toggles + text), `blacklist`, `user_warns`, `rss_feeds`, `rss_sent` (dedup), `bot_config` (key-value for API keys), `platform_credentials`, `psn_library_games`.

Groups must be explicitly activated (`is_active=True`) via Web Admin before most features engage.

## Adding a Plugin

Create `plugins/my_feature.py` — it is auto-loaded on next start, no changes to `main.py` needed:

```python
from telegram.ext import Application, CommandHandler
from bot.router import registry

async def my_command(update, context):
    await update.effective_message.reply_text("Hello!")

def setup(application: Application) -> None:
    registry.register_command("mycommand", my_command, "Does something")
    application.add_handler(CommandHandler("mycommand", my_command))
```

## Key Conventions

- Bot responses in groups default to **Chinese** (error messages, status text). This is intentional — the primary audience is Chinese-speaking users.
- API keys read at call time (`await get_bot_config("claude_api_key")`), not at startup — never cache them at module load.
- When adding a new DB column, add it to the ORM model **and** to the `migrations` list in `database.py:_migrate()`.
- The `bot_config` table is the single source of truth for all runtime-configurable secrets. Do not hardcode fallback key values.
- `ffmpeg` is required (installed in Docker) for media processing via yt-dlp.
