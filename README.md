# Ponygram Bot

A modular, async Telegram bot with group management, AI dialogue (Claude), media parsing, RSS, and PlayStation game queries.

**Web Admin UI built-in** — manage everything from a browser, no SSH required.

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/folooox/ponygram
cd ponygram
pip install -r requirements.txt

# 2. Copy and edit config
cp .env.example .env
nano .env   # at minimum: BOT_TOKEN + OWNER_ID + WEB_SECRET

# 3. Run
python main.py
```

Open `http://localhost:8080` → log in with `WEB_SECRET` → start configuring.

---

## Environment Variables

Copy `.env.example` → `.env`:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | ✅ | Your Telegram user ID (send `/start` to [@userinfobot](https://t.me/userinfobot)) |
| `DATABASE_URL` | — | SQLAlchemy URL (default: `sqlite+aiosqlite:///data/ponygram.db`) |
| `LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |
| `WEBHOOK_URL` | — | Set to enable webhook mode (e.g. `https://yourdomain.com/webhook`) |
| `WEBHOOK_PORT` | — | Webhook port (default `8443`) |
| `RSS_INTERVAL` | — | Feed poll interval in minutes (min 5, default 15) |
| `WEB_ENABLED` | — | `true` to start the Web Admin UI (default `false`) |
| `WEB_SECRET` | ✅ if web | Strong password for the Web Admin login page |
| `WEB_HOST` | — | Bind host (default `0.0.0.0`) |
| `WEB_PORT` | — | Web Admin port (default `8080`) |

> **API Keys** (Claude, TMDB, Last.fm, RAWG, PSN) are **not** set in `.env`.
> Configure them in **Web Admin → Settings** after the bot starts — no restart needed.

---

## API Keys — Where to Get Them

### 🤖 Claude API Key

**Used for:** AI dialogue — triggered by @mention / "pony " prefix / reply to bot

**How to get:**
1. Go to **[console.anthropic.com](https://console.anthropic.com)**
2. Sign up / log in → **API Keys** in the left sidebar → **Create Key**
3. Give it a name (e.g. "ponygram") → **Create Key**
4. Copy the key immediately — it is **only shown once**

**Format:** `sk-ant-api03-` followed by ~95 alphanumeric characters
```
sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX-XXXXXXXXXXXXXXXX
```

---

### 🎬 TMDB API Key

**Used for:** `@bot movie <title>` and `@bot tv <title>` inline search

**How to get:**
1. Go to **[themoviedb.org](https://www.themoviedb.org)** → sign up for free
2. Click your avatar → **Settings** → **API** (left sidebar)
3. Under "Request an API Key" → **Developer** → accept terms
4. Fill in: App name = "Ponygram Bot", App URL = your server IP, Summary = "personal Telegram bot"
5. Copy **API Key (v3 auth)** — the shorter 32-character key, NOT the "API Read Access Token"

**Format:** 32 lowercase hex characters (no dashes, no prefix)
```
a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
```

---

### 🎵 Last.fm API Key

**Used for:** `@bot music <artist - track>` and `@bot artist <name>` inline search

**How to get:**
1. Sign in at **[last.fm](https://www.last.fm)**
2. Go to **[last.fm/api/account/create](https://www.last.fm/api/account/create)**
3. Fill in: Application name = "Ponygram Bot", Description = "personal Telegram bot"
4. Submit → copy the **API key** (not the Shared Secret)

**Format:** 32 lowercase hex characters
```
a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
```

---

### 🎮 RAWG.io API Key

**Used for:** `@bot game <title>` — PS4/PS5 game search with cover art, ratings, Metacritic scores

**How to get:**
1. Go to **[rawg.io/apidocs](https://rawg.io/apidocs)**
2. Click **Get API Key** → sign up for free (no credit card)
3. Your API key is shown on the page immediately after registration

**Free tier:** 200,000 requests/month — sufficient for personal use.

**Format:** 40 lowercase hex characters
```
a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2
```

---

### 🕹️ PSN NPSSO Token

**Used for:** `/psn <id>` (user profile, trophy level, platinum count) and `/trophy <id> <game>`

The NPSSO is a long-lived session cookie from your PlayStation Network account. The bot automatically exchanges it for short-lived API tokens (valid 1h, refreshed silently).

**Step-by-step:**

**Step 1** — Log in to PSN in your browser:
```
https://store.playstation.com
```

**Step 2** — In the same browser, open a new tab and navigate to:
```
https://ca.account.sony.com/api/v1/ssocookie
```

**Step 3** — You will see a raw JSON response:
```json
{"npsso":"Abcdefghijklmnopqrstuvwxyz123456ABCDEFGHIJKLMNOPQRSTUVWXYZ789012"}
```

**Step 4** — Copy only the value (64 characters between the quotes, without the quotes).

**Step 5** — Paste into **Web Admin → Settings → PSN NPSSO Token** → Save.

**Format:** 64 alphanumeric characters, mixed case
```
Abcdefghijklmnopqrstuvwxyz123456ABCDEFGHIJKLMNOPQRSTUVWXYZ789012
```

**⚠️ Notes:**
- Expires after ~2 months or when you sign out of PSN on any device
- If `/psn` stops working, repeat the steps to get a fresh token and paste it again
- The bot only **reads** data — it never modifies your PSN account
- Use your own PSN account's NPSSO (it's tied to your login session)

---

### 🍪 Platform Cookies (media parsing)

**Used for:** downloading restricted content from Instagram, Twitter/X, Bilibili, 抖音, etc.

Without cookies, many platforms block anonymous requests. Cookies let the bot act as a logged-in user when parsing media URLs.

**How to export cookies (works for all platforms):**

1. Install **[Cookie-Editor](https://cookie-editor.com/)** browser extension (Chrome or Firefox)
2. Log in to the platform in your browser (e.g. instagram.com)
3. Click the Cookie-Editor icon in your toolbar
4. Click **Export** → **Header String**
5. Copy the result (it looks like: `sessionid=abc123; csrftoken=xyz456; ds_user_id=789...`)
6. Paste into **Web Admin → Settings** → the corresponding platform field

**Supported:** Instagram · Twitter/X · Bilibili · 抖音 · TikTok · 快手 · 小红书 · YouTube

---

## Commands Reference

### 👤 General

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | List commands |
| `/id` | Your Telegram ID |
| `/id @user` | Another user's info |
| `/whois` | Detailed user info from DB |
| `/rules` | Show group rules (configured via Web Admin) |

### 🔨 Moderation (group admins only)

| Command | Description |
|---|---|
| `/mute [@user\|id] [1h\|30m\|2d]` | Mute a user |
| `/unmute [@user\|id]` | Unmute |
| `/kick [@user\|id]` | Remove from group (can rejoin) |
| `/ban [@user\|id] [reason]` | Permanently ban |
| `/unban [@user\|id]` | Unban |
| `/warn [@user\|id] [reason]` | Warn (auto-bans at warn limit) |
| `/warns [@user\|id]` | Show warning history |
| `/clearwarns [@user\|id]` | Clear all warnings |
| `/pin [loud]` | Pin replied-to message |
| `/unpin` | Unpin |

All moderation commands work by **replying to a message** or passing `@username` / user ID.

### 🤖 AI Dialogue

No slash commands. The bot responds when you:
- **@mention** it in a group
- Start a message with **`pony `** (case-insensitive)
- **Reply** to a message the bot sent

Requires `claude_api_key` in Web Admin → Settings.

### 🎮 PlayStation

| Command | Requires | Description |
|---|---|---|
| `/psn <psn_id>` | PSN NPSSO | User profile (trophy level, platinums, recent games) |
| `/trophy <psn_id> <game>` | PSN NPSSO | Trophy progress for a game |
| `/psprice <title>` | — | PS Store price + history low (US + CN via PSPrices.com) |

### 🔍 Inline Search (`@bot <prefix> <query>`)

Type in any chat:

| Prefix | Requires | Description |
|---|---|---|
| `movie <title>` | TMDB Key | Movie search |
| `tv <title>` | TMDB Key | TV show search |
| `book <query>` | — | Google Books |
| `music <artist - track>` | Last.fm Key | Track search |
| `artist <name>` | Last.fm Key | Artist info + top tracks |
| `game <title>` | RAWG Key | PS4/PS5 game search |
| `psn <psn_id>` | PSN NPSSO | PSN user profile card |

---

## Web Admin UI

Enable in `.env`:
```env
WEB_ENABLED=true
WEB_SECRET=your-strong-random-password
WEB_PORT=8080
```

### Pages

| Page | Description |
|---|---|
| `/` | Dashboard — stats + **live service health checks** |
| `/groups` | All groups; activate new groups by ID or `t.me/` share link |
| `/groups/<id>` | Group settings (toggles, thresholds, welcome/goodbye/rules text) |
| `/blacklist` | Global ban list |
| `/rss` | RSS subscriptions across all chats |
| `/settings` | API keys + PSN token + platform cookies (with inline help for each) |

### Service Health Panel (Dashboard)

The dashboard shows a real-time status panel for every configured service:

- **🟢 Configured** — key exists in the database
- **🔴 Not configured** — feature is unavailable until set up
- **✅ OK** — key verified by a live test API call
- **❌ Failed** — key is set but invalid or the service is unreachable

Press **Test** next to any service to make a live API call and verify the key actually works.

### Per-Group Settings

**Toggles:** Welcome · Goodbye · Join verification · Anti-spam · Anti-ad · Media URL auto-detect · AI auto-reply

**Thresholds:** Verification timeout · Anti-spam rate (msgs/window) · Warn auto-ban limit

**Text fields:** Welcome message · Goodbye message · Group rules

> **Security:** `WEB_SECRET` is HMAC-SHA256 signed into a session cookie.
> Never expose port 8080 publicly without a reverse proxy + TLS.

---

## Project Structure

```
ponygram/
├── bot/
│   ├── config.py           # .env parsing
│   ├── database.py         # SQLAlchemy async (SQLite + auto-migration)
│   ├── logger.py           # structlog structured logging
│   ├── permissions.py      # admin_only / group_only decorators
│   ├── router.py           # Command registry
│   ├── cache.py            # TTL in-memory cache
│   └── plugin_loader.py    # Auto-loads modules/ + plugins/
├── modules/
│   ├── start.py            # /start, /help
│   ├── admin.py            # Moderation commands + /rules
│   ├── welcome.py          # Welcome/goodbye + join verification
│   ├── antispam.py         # Rate-limit + anti-invite-link filter
│   ├── userinfo.py         # /id, /whois
│   ├── rss.py              # RSS subscriptions (background scheduler)
│   ├── movie.py            # TMDB movie/TV
│   ├── book.py             # Google Books
│   ├── music.py            # Last.fm
│   ├── media.py            # yt-dlp / ParseHub media download
│   ├── ai_chat.py          # Claude AI dialogue
│   ├── inline.py           # @bot inline queries
│   ├── psn.py              # PS game search, PSN profile, trophies
│   └── cookie_manager.py   # Platform cookie auto-refresh
├── web/
│   ├── app.py              # FastAPI web admin (all routes)
│   └── templates/          # Jinja2 + Bootstrap 5 dark theme
├── plugins/                # Drop custom .py plugins here (auto-loaded)
├── data/                   # SQLite database (git-ignored)
├── logs/                   # Log files (git-ignored)
├── main.py                 # Entry point
├── .env.example
└── requirements.txt
```

---

## Deployment

### Docker (recommended)

```bash
cp .env.example .env
# Edit: BOT_TOKEN, OWNER_ID, WEB_ENABLED=true, WEB_SECRET=your-password

docker compose up -d
docker compose logs -f
```

### systemd

```bash
sudo useradd -r -s /bin/false ponygram
sudo mkdir -p /opt/ponygram && sudo cp -r . /opt/ponygram
sudo chown -R ponygram:ponygram /opt/ponygram
sudo -u ponygram python3 -m venv /opt/ponygram/.venv
sudo -u ponygram /opt/ponygram/.venv/bin/pip install -r /opt/ponygram/requirements.txt
sudo cp /opt/ponygram/.env.example /opt/ponygram/.env
sudo nano /opt/ponygram/.env   # set BOT_TOKEN etc.
sudo cp deploy/ponygram.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ponygram
sudo journalctl -u ponygram -f
```

### Reverse Proxy (nginx + TLS)

```nginx
server {
    listen 443 ssl;
    server_name admin.yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/admin.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.yourdomain.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Adding a Custom Plugin

Create `plugins/my_feature.py`:

```python
from telegram.ext import Application, CommandHandler
from bot.router import registry

async def my_command(update, context):
    await update.effective_message.reply_text("Hello from my plugin!")

def setup(application: Application) -> None:
    registry.register_command("mycommand", my_command, "Does something cool")
    application.add_handler(CommandHandler("mycommand", my_command))
```

Picked up automatically on next start — no changes to `main.py` needed.
