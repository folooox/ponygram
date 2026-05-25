# Ponygram Bot

A modular, async Telegram bot built with [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21.

## Quick Start

```bash
# 1. Clone and install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — at minimum set BOT_TOKEN (get one from @BotFather)

# 3. Run
python main.py
```

## Features

### Group Management
| Command | Description |
|---|---|
| `/mute [@user\|id] [1h\|30m\|2d]` | Restrict a user from sending messages |
| `/unmute [@user\|id]` | Lift a mute |
| `/kick [@user\|id]` | Remove a user (they can rejoin) |
| `/ban [@user\|id] [reason]` | Permanently ban a user |
| `/unban [@user\|id]` | Lift a ban |
| `/warn [@user\|id] [reason]` | Warn a user; auto-bans at warn limit |
| `/warns [@user\|id]` | Show warnings for a user |
| `/clearwarns [@user\|id]` | Clear all warnings for a user |
| `/warnlimit <n>` | Set auto-ban threshold (default: 3) |
| `/pin [loud]` | Pin the replied-to message |
| `/unpin` | Unpin a message |
| `/gblacklist [@user\|id]` | Add to global bot blacklist (owner only) |
| `/gunblacklist [@user\|id]` | Remove from global blacklist (owner only) |

All moderation commands work by **replying to a message** or passing an integer user ID or `@username`.

### Welcome & Verification
| Command | Description |
|---|---|
| `/setwelcome <text>` | Set welcome message (supports `{name}`, `{username}`, `{chat}`, `{id}`) |
| `/delwelcome` | Disable welcome message |
| `/setgoodbye <text>` | Set goodbye message |
| `/delgoodbye` | Disable goodbye message |
| `/verification on\|off` | Toggle join verification (CAPTCHA button) |
| `/verificationtimeout <secs>` | Kick unverified users after N seconds |

### Anti-Spam & Anti-Ad
| Command | Description |
|---|---|
| `/antispam on\|off` | Toggle rate-limit muting |
| `/antispam threshold <n> <secs>` | N messages per window before auto-mute |
| `/antiad on\|off` | Block Telegram group invite links |

### RSS Subscriptions
| Command | Description |
|---|---|
| `/rss add <url> [label]` | Subscribe current chat to a feed |
| `/rss del <id>` | Unsubscribe a feed |
| `/rss list` | List active subscriptions |
| `/rss pause <id>` | Pause polling |
| `/rss resume <id>` | Resume polling |
| `/rss interval <minutes>` | Set poll interval (min 5, default 15) |

### Media Queries (requires API keys)
| Command | Description | Key |
|---|---|---|
| `/movie <title>` | Search movies on TMDB | `TMDB_API_KEY` |
| `/tv <title>` | Search TV shows on TMDB | `TMDB_API_KEY` |
| `/book <query>` | Search books via Google Books | — |
| `/music <artist - track>` | Search a track on Last.fm | `LASTFM_API_KEY` |
| `/artist <name>` | Artist info + top tracks | `LASTFM_API_KEY` |

### Media Download (yt-dlp)
| Command | Description |
|---|---|
| `/dl <url>` | Download video/audio and send to chat |
| `/dlinfo <url>` | Show media info without downloading |
| `/dlmode on\|off` | Toggle auto-detection of media URLs (admin) |

Supports YouTube, Bilibili, TikTok / Douyin, Instagram, Twitter/X, and 1 000+ other sites.
Files over 50 MB fall back to a thumbnail + info card.

### AI Dialogue (Claude — requires `CLAUDE_API_KEY`)
| Command | Description |
|---|---|
| `/chat <message>` | Chat with Claude AI (multi-turn, per-user history) |
| `/ask <message>` | Alias for `/chat` |
| `/clearchat` | Reset your conversation history |
| `/aichat on\|off` | Auto-reply when the bot is @mentioned (admin) |

Uses `claude-opus-4-7` with adaptive thinking and live streaming (the reply updates progressively as Claude generates text).

### Utility
| Command | Description |
|---|---|
| `/id` | Show your Telegram ID and info |
| `/id [@user\|reply]` | Show another user's info |
| `/whois` | Detailed user info from the database |
| `/start` | Intro message |
| `/help` | List all available commands |

---

## Project Structure

```
ponygram/
├── bot/                    # Core framework
│   ├── config.py           # Environment-based configuration
│   ├── database.py         # SQLAlchemy async DB layer (SQLite)
│   ├── logger.py           # structlog structured logging
│   ├── error_handler.py    # Global Telegram error handler
│   ├── permissions.py      # admin_only / owner_only / group_only decorators
│   ├── router.py           # Command registry & handler setup
│   ├── cache.py            # TTL in-memory cache for API results
│   └── plugin_loader.py    # Auto-loads modules/ and plugins/
├── modules/                # Built-in feature modules
│   ├── start.py            # /start, /help
│   ├── admin.py            # Moderation + warn system + pin/unpin
│   ├── welcome.py          # Welcome/goodbye + join verification
│   ├── antispam.py         # Rate-limit + anti-invite-link filter
│   ├── userinfo.py         # /id, /whois
│   ├── rss.py              # RSS subscriptions
│   ├── movie.py            # TMDB movie/TV search
│   ├── book.py             # Google Books search
│   ├── music.py            # Last.fm track/artist search
│   ├── media.py            # yt-dlp download
│   └── ai_chat.py          # Claude AI dialogue
├── plugins/                # Drop third-party .py plugins here
├── data/                   # SQLite database (git-ignored)
├── logs/                   # Log files (git-ignored)
├── main.py                 # Entry point
├── .env.example            # Environment variable template
└── requirements.txt
```

## Environment Variables

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Token from @BotFather |
| `OWNER_ID` | ✅ | Your Telegram user ID |
| `ADMIN_IDS` | — | Comma-separated additional admin IDs |
| `DATABASE_URL` | — | SQLAlchemy URL (default: SQLite) |
| `LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `WEBHOOK_URL` | — | Set to enable webhook mode |
| `WEBHOOK_PORT` | — | Webhook port (default 8443) |
| `RSS_INTERVAL` | — | Feed poll interval in minutes (default 15) |
| `TMDB_API_KEY` | — | For `/movie` and `/tv` |
| `LASTFM_API_KEY` | — | For `/music` and `/artist` |
| `CLAUDE_API_KEY` | — | For `/chat` and AI auto-reply |
| `DL_MAX_MB` | — | Max upload size in MB (default 49) |

## Adding a Custom Plugin

Create `plugins/my_feature.py` with a `setup(application)` function:

```python
from telegram.ext import Application, CommandHandler
from bot.router import registry

async def my_command(update, context):
    await update.effective_message.reply_text("Hello!")

def setup(application: Application) -> None:
    registry.register_command("mycommand", my_command, "Does something cool")
    application.add_handler(CommandHandler("mycommand", my_command))
```

The plugin is picked up automatically on the next start — no changes to `main.py` needed.

## Roadmap

| Phase | Feature | Status |
|---|---|---|
| 1 | Base framework (config, logging, routing, plugin loader) | ✅ Done |
| 2 | Group management (welcome, verification, anti-spam) | ✅ Done |
| 3 | RSS subscriptions | ✅ Done |
| 4 | Media queries (TMDB, Google Books, Last.fm) | ✅ Done |
| 5 | Media download via yt-dlp | ✅ Done |
| 6 | AI dialogue via Claude | ✅ Done |
| Polish | Warn system, pin/unpin, live streaming, bug fixes | ✅ Done |
| 7 | Deployment (Docker, systemd) | Planned |
