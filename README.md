# Ponygram Bot

A modular, async Telegram bot built with [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21.

## Quick Start

```bash
# 1. Clone and install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env and set BOT_TOKEN (get one from @BotFather)

# 3. Run
python main.py
```

## Project Structure

```
ponygram/
├── bot/                  # Core framework
│   ├── config.py         # Environment-based configuration
│   ├── logger.py         # structlog logging setup
│   ├── error_handler.py  # Global Telegram error handler
│   ├── router.py         # Command registry & handler setup
│   └── plugin_loader.py  # Dynamic module/plugin loader
├── modules/              # Built-in feature modules
│   └── start.py          # /start and /help commands
├── plugins/              # Optional third-party plugins (drop .py files here)
├── data/                 # SQLite database (git-ignored)
├── logs/                 # Log files (git-ignored)
├── main.py               # Entry point
├── .env.example          # Environment variable template
└── requirements.txt
```

## Adding a New Module

Create `modules/my_feature.py` with a `setup(application)` function:

```python
from telegram.ext import Application, CommandHandler
from bot.router import registry

async def my_command(update, context):
    await update.effective_message.reply_text("Hello!")

def setup(application: Application) -> None:
    registry.register_command("mycommand", my_command, "Does something cool")
    application.add_handler(CommandHandler("mycommand", my_command))
```

The module is picked up automatically on next start — no changes to `main.py` needed.

## Roadmap

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Base framework (config, logging, routing, plugin loader) | ✅ Done |
| 2 | User & group management (permissions, welcome, anti-spam) | Planned |
| 3 | RSS subscriptions | Planned |
| 4 | Query features (TMDB, books, music) | Planned |
| 5 | Media parsing (YouTube, Bilibili, TikTok…) | Planned |
| 6 | AI chat (Claude / OpenAI adapter) | Planned |
| 7 | Deployment (Docker, systemd) | Planned |

## Environment Variables

See `.env.example` for the full list. Required: `BOT_TOKEN`.
