#!/usr/bin/env bash
# ============================================================
#  Ponygram Bot — Installer & Setup Wizard
#
#  One-liner (any method works):
#    curl -fsSL https://raw.githubusercontent.com/folooox/ponygram/main/install.sh | bash
#    bash <(curl -fsSL https://raw.githubusercontent.com/folooox/ponygram/main/install.sh)
#    wget -qO- https://raw.githubusercontent.com/folooox/ponygram/main/install.sh | bash
# ============================================================
set -euo pipefail

# ── Pipe-execution guard ─────────────────────────────────────
# When piped (curl|bash or wget|bash), stdin is the pipe itself,
# so interactive `read` calls get EOF.  Detect this case, download
# the script to a temp file, and re-exec with a proper TTY.
if [[ ! -t 0 ]]; then
    SCRIPT_URL="https://raw.githubusercontent.com/folooox/ponygram/main/install.sh"
    TMP=$(mktemp /tmp/ponygram-install.XXXXXX.sh)
    if command -v curl &>/dev/null; then
        curl -fsSL "$SCRIPT_URL" -o "$TMP"
    elif command -v wget &>/dev/null; then
        wget -qO "$TMP" "$SCRIPT_URL"
    else
        echo "Error: curl or wget is required to run this installer." >&2
        exit 1
    fi
    chmod +x "$TMP"
    # Re-exec with stdin explicitly from /dev/tty so interactive read() works
    exec bash "$TMP" "$@" </dev/tty
fi

# ── Colours ─────────────────────────────────────────────────
RED='\033[0;31m';   GREEN='\033[0;32m';  YELLOW='\033[1;33m'
BLUE='\033[0;34m';  CYAN='\033[0;36m';   BOLD='\033[1m';  NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  ·${NC}  $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*" >&2; }
step() { echo -e "\n${BOLD}${BLUE}▶ $*${NC}"; }
ask()  { echo -en "${CYAN}  ?${NC}  $* "; }

REPO_URL="https://github.com/folooox/ponygram"
REPO_RAW="https://raw.githubusercontent.com/folooox/ponygram/main"
INSTALL_DIR="${PONYGRAM_DIR:-$HOME/ponygram}"
PYTHON_MIN_MINOR=10   # require 3.10+

# ── Banner ───────────────────────────────────────────────────
clear 2>/dev/null || true
echo -e "${BOLD}${BLUE}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║         PONYGRAM BOT INSTALLER           ║"
echo "  ║         github.com/folooox/ponygram      ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo "  This wizard will:"
echo "    • Check system requirements"
echo "    • Install missing dependencies"
echo "    • Walk you through basic configuration"
echo "    • Get the bot running"
echo ""

# ── Helpers ──────────────────────────────────────────────────
confirm() {
    local prompt="${1:-Continue?} [y/N] "
    ask "$prompt"
    read -r ans
    [[  "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

confirm_default_yes() {
    local prompt="${1:-Continue?} [Y/n] "
    ask "$prompt"
    read -r ans
    [[ -z "$ans" || "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

read_input() {
    local prompt="$1" var="$2" default="${3:-}"
    if [[ -n "$default" ]]; then
        ask "$prompt [${default}]:"
    else
        ask "$prompt:"
    fi
    read -r "$var"
    if [[ -z "${!var}" && -n "$default" ]]; then
        printf -v "$var" '%s' "$default"
    fi
}

read_secret() {
    local prompt="$1" var="$2"
    ask "$prompt (hidden):"
    read -rs "$var"
    echo ""
}

command_exists() { command -v "$1" &>/dev/null; }

# Download a URL to stdout or a file; prefers curl, falls back to wget
_download() {
    local url="$1" dest="${2:-}"
    if command_exists curl; then
        if [[ -n "$dest" ]]; then curl -fsSL "$url" -o "$dest"
        else curl -fsSL "$url"; fi
    elif command_exists wget; then
        if [[ -n "$dest" ]]; then wget -qO "$dest" "$url"
        else wget -qO- "$url"; fi
    else
        err "curl or wget not found — cannot download files."; exit 1
    fi
}

# Run a command with sudo if not already root
_sudo() {
    if [[ $EUID -eq 0 ]]; then "$@"
    elif command_exists sudo; then sudo "$@"
    else err "Need root or sudo to run: $*"; exit 1
    fi
}

detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command_exists apt-get; then echo "debian"
        elif command_exists dnf;     then echo "fedora"
        elif command_exists yum;     then echo "centos"
        elif command_exists pacman;  then echo "arch"
        else                              echo "linux"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    else
        echo "unknown"
    fi
}

# ── Step 1 : Locate / clone repository ──────────────────────
step "1/6  Locating repository"

IN_REPO=false
if [[ -f "$(pwd)/main.py" && -f "$(pwd)/requirements.txt" && -d "$(pwd)/modules" ]]; then
    INSTALL_DIR="$(pwd)"
    IN_REPO=true
    ok "Running from inside the Ponygram repo: $INSTALL_DIR"
elif [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/main.py" ]]; then
    ok "Found existing install at $INSTALL_DIR"
    IN_REPO=true
else
    info "Cloning repository into $INSTALL_DIR …"
    if ! command_exists git; then
        OS_PRE=$(detect_os)
        warn "git not found — attempting to install …"
        case "$OS_PRE" in
            debian) _sudo apt-get update -qq && _sudo apt-get install -y git ;;
            fedora) _sudo dnf install -y git ;;
            centos) _sudo yum install -y git ;;
            arch)   _sudo pacman -Sy --noconfirm git ;;
            macos)  command_exists brew && brew install git || { err "Install git manually."; exit 1; } ;;
            *) err "git is not installed. Please install git and re-run."; exit 1 ;;
        esac
    fi
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    ok "Repository cloned to $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Step 2 : System requirements ────────────────────────────
step "2/6  Checking system requirements"
OS=$(detect_os)
info "Detected OS: $OS"

# Python
if ! command_exists python3; then
    err "python3 not found. Please install Python 3.${PYTHON_MIN_MINOR}+ and re-run."
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")

if [[ "$PY_MAJOR" -lt 3 || ("$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt "$PYTHON_MIN_MINOR") ]]; then
    err "Python 3.${PYTHON_MIN_MINOR}+ required, found $PY_VER"
    exit 1
fi
ok "Python $PY_VER"

# pip
if ! python3 -m pip --version &>/dev/null; then
    warn "pip not found, attempting to install …"
    python3 -m ensurepip --upgrade 2>/dev/null || {
        err "Could not install pip. Please install python3-pip manually."
        exit 1
    }
fi
ok "pip $(python3 -m pip --version | awk '{print $2}')"

# ffmpeg (required by yt-dlp for video merging)
FFMPEG_OK=false
if command_exists ffmpeg; then
    ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
    FFMPEG_OK=true
else
    warn "ffmpeg not found — required for video+audio merging"
    case "$OS" in
        debian)
            if confirm "  Install ffmpeg via apt?"; then
                _sudo apt-get update -qq && _sudo apt-get install -y ffmpeg
                FFMPEG_OK=true; ok "ffmpeg installed"
            fi ;;
        fedora)
            if confirm "  Install ffmpeg via dnf?"; then
                _sudo dnf install -y --allowerasing ffmpeg
                FFMPEG_OK=true; ok "ffmpeg installed"
            fi ;;
        centos)
            if confirm "  Install ffmpeg via yum (requires EPEL)?"; then
                _sudo yum install -y epel-release
                _sudo yum install -y ffmpeg
                FFMPEG_OK=true; ok "ffmpeg installed"
            fi ;;
        arch)
            if confirm "  Install ffmpeg via pacman?"; then
                _sudo pacman -Sy --noconfirm ffmpeg
                FFMPEG_OK=true; ok "ffmpeg installed"
            fi ;;
        macos)
            if command_exists brew; then
                if confirm "  Install ffmpeg via Homebrew?"; then
                    brew install ffmpeg
                    FFMPEG_OK=true; ok "ffmpeg installed"
                fi
            else
                warn "Homebrew not found. Install ffmpeg manually: https://ffmpeg.org/download.html"
            fi ;;
        *)
            warn "Please install ffmpeg manually: https://ffmpeg.org/download.html" ;;
    esac
    $FFMPEG_OK || warn "Continuing without ffmpeg — some downloads may fail to merge audio/video"
fi

# Docker (optional)
DOCKER_OK=false
if command_exists docker; then
    ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
    DOCKER_OK=true
fi

COMPOSE_OK=false
if command_exists docker && docker compose version &>/dev/null 2>&1; then
    ok "Docker Compose (plugin)"
    COMPOSE_OK=true
elif command_exists docker-compose; then
    ok "docker-compose $(docker-compose --version | awk '{print $3}' | tr -d ',')"
    COMPOSE_OK=true
fi

# ── Step 3 : Choose install mode ─────────────────────────────
step "3/6  Installation mode"

INSTALL_MODE=""
if $COMPOSE_OK; then
    echo ""
    echo "  How would you like to run Ponygram?"
    echo "    1) Docker Compose  (recommended — isolated, easy to update)"
    echo "    2) Bare-metal venv (Python virtualenv + optional systemd service)"
    echo ""
    ask "Choose [1/2, default 1]:"
    read -r mode_choice
    case "${mode_choice:-1}" in
        2) INSTALL_MODE="venv" ;;
        *) INSTALL_MODE="docker" ;;
    esac
else
    info "Docker Compose not available — using bare-metal venv"
    INSTALL_MODE="venv"
fi
ok "Install mode: $INSTALL_MODE"

# ── Install Python dependencies (bare-metal) ─────────────────
if [[ "$INSTALL_MODE" == "venv" ]]; then
    step "3b/6  Setting up Python virtualenv"

    VENV_DIR="$INSTALL_DIR/.venv"
    if [[ -d "$VENV_DIR" ]]; then
        info "Virtualenv already exists at $VENV_DIR"
    else
        python3 -m venv "$VENV_DIR"
        ok "Created virtualenv at $VENV_DIR"
    fi

    PYTHON="$VENV_DIR/bin/python"
    PIP="$VENV_DIR/bin/pip"

    info "Installing Python packages (this may take a minute) …"
    "$PIP" install --quiet --upgrade pip wheel
    "$PIP" install --quiet -r "$INSTALL_DIR/requirements.txt"
    ok "Python packages installed"

    # Verify key imports
    for pkg in telegram anthropic fastapi yt_dlp feedparser; do
        if "$PYTHON" -c "import $pkg" 2>/dev/null; then
            ok "  $pkg"
        else
            warn "  $pkg import failed — check requirements.txt"
        fi
    done
else
    PYTHON="python3"
fi

# ── Step 4 : Configuration wizard ────────────────────────────
step "4/6  Configuration"

ENV_FILE="$INSTALL_DIR/.env"
ENV_EXAMPLE="$INSTALL_DIR/.env.example"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists at $ENV_FILE"
    if ! confirm "  Reconfigure it? (no = keep existing)"; then
        ok "Keeping existing .env"
        SKIP_CONFIG=true
    else
        cp "$ENV_FILE" "${ENV_FILE}.bak"
        info "Backup saved to ${ENV_FILE}.bak"
        SKIP_CONFIG=false
    fi
else
    SKIP_CONFIG=false
fi

if ! $SKIP_CONFIG; then
    echo ""
    echo -e "  ${BOLD}Required settings${NC}"
    echo "  ─────────────────────────────────────────────"

    # BOT_TOKEN
    while true; do
        read_secret "Bot token from @BotFather" BOT_TOKEN
        if [[ "${BOT_TOKEN}" =~ ^[0-9]+:[A-Za-z0-9_-]{35,}$ ]]; then
            ok "Token format looks valid"
            break
        else
            warn "Token looks wrong (expected format: 123456789:ABCDEF…). Try again."
        fi
    done

    # OWNER_ID
    while true; do
        read_input "Your Telegram user ID (integer)" OWNER_ID
        if [[ "${OWNER_ID}" =~ ^[0-9]+$ ]]; then
            ok "Owner ID: $OWNER_ID"
            break
        else
            warn "Must be a plain integer. Get it by messaging @userinfobot"
        fi
    done

    echo ""
    echo -e "  ${BOLD}Optional API keys${NC}  (press Enter to skip)"
    echo "  ─────────────────────────────────────────────"

    read_secret "Claude API key (for AI chat — sk-ant-…)" CLAUDE_API_KEY
    [[ -n "$CLAUDE_API_KEY" ]] && ok "Claude key set" || info "Claude key skipped"

    read_secret "TMDB API key (for movie/TV inline search)" TMDB_API_KEY
    [[ -n "$TMDB_API_KEY" ]] && ok "TMDB key set" || info "TMDB key skipped"

    read_secret "Last.fm API key (for music inline search)" LASTFM_API_KEY
    [[ -n "$LASTFM_API_KEY" ]] && ok "Last.fm key set" || info "Last.fm key skipped"

    echo ""
    echo -e "  ${BOLD}Web Admin UI${NC}"
    echo "  ─────────────────────────────────────────────"

    WEB_ENABLED="false"
    WEB_SECRET=""
    WEB_PORT="8080"
    if confirm_default_yes "  Enable Web Admin UI?"; then
        WEB_ENABLED="true"
        while true; do
            read_secret "  Admin password (WEB_SECRET)" WEB_SECRET
            if [[ ${#WEB_SECRET} -ge 8 ]]; then
                ok "Password set (${#WEB_SECRET} chars)"
                break
            else
                warn "Password must be at least 8 characters"
            fi
        done
        # Pick a free port
        DEFAULT_PORT="8080"
        while ss -tlnp 2>/dev/null | grep -q ":${DEFAULT_PORT} " || \
              lsof -iTCP:"${DEFAULT_PORT}" -sTCP:LISTEN &>/dev/null 2>&1; do
            DEFAULT_PORT=$((DEFAULT_PORT + 1))
        done
        [[ "$DEFAULT_PORT" != "8080" ]] && warn "Port 8080 in use — suggesting $DEFAULT_PORT"
        read_input "  Web UI port" WEB_PORT "$DEFAULT_PORT"
        # Final check: warn if chosen port is still busy
        if ss -tlnp 2>/dev/null | grep -q ":${WEB_PORT} " || \
           lsof -iTCP:"${WEB_PORT}" -sTCP:LISTEN &>/dev/null 2>&1; then
            warn "Port $WEB_PORT appears to be in use — container may fail to start"
        fi
    fi

    echo ""
    echo -e "  ${BOLD}Advanced (press Enter for defaults)${NC}"
    echo "  ─────────────────────────────────────────────"
    read_input "  Log level" LOG_LEVEL "INFO"
    read_input "  RSS poll interval (minutes, min 5)" RSS_INTERVAL "15"

    # Write .env
    cat > "$ENV_FILE" <<ENVEOF
# Ponygram Bot — generated by install.sh
# Edit any value and restart the bot to apply changes.

# ── Core ─────────────────────────────────────────────────────
BOT_TOKEN=${BOT_TOKEN}
OWNER_ID=${OWNER_ID}
ADMIN_IDS=

# ── Database ─────────────────────────────────────────────────
DATABASE_URL=sqlite+aiosqlite:///data/ponygram.db

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL=${LOG_LEVEL}
LOG_FILE=logs/ponygram.log

# ── Webhook (leave empty to use long-polling) ─────────────────
WEBHOOK_URL=
WEBHOOK_PORT=8443

# ── RSS ───────────────────────────────────────────────────────
RSS_INTERVAL=${RSS_INTERVAL}

# ── Optional API keys ─────────────────────────────────────────
# These can also be set via Web Admin UI → Settings
TMDB_API_KEY=${TMDB_API_KEY}
LASTFM_API_KEY=${LASTFM_API_KEY}
CLAUDE_API_KEY=${CLAUDE_API_KEY}

# ── Media download ────────────────────────────────────────────
DL_MAX_MB=49

# ── Web Admin UI ──────────────────────────────────────────────
WEB_ENABLED=${WEB_ENABLED}
WEB_HOST=0.0.0.0
WEB_PORT=${WEB_PORT}
WEB_SECRET=${WEB_SECRET}
ENVEOF

    # Secure the env file
    chmod 600 "$ENV_FILE"
    ok ".env written to $ENV_FILE"
fi

# Create required directories
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs" "$INSTALL_DIR/plugins"
ok "Directories: data/, logs/, plugins/"

# ── Step 5 : Start / deploy ──────────────────────────────────
step "5/6  Deployment"

if [[ "$INSTALL_MODE" == "docker" ]]; then
    echo ""
    info "Building Docker image and starting container …"
    cd "$INSTALL_DIR"
    if command_exists docker && docker compose version &>/dev/null 2>&1; then
        docker compose up -d --build
    else
        docker-compose up -d --build
    fi
    ok "Container started"
    info "View logs: docker compose logs -f"

else
    # Bare-metal: optional systemd setup
    echo ""
    SYSTEMD_OK=false
    if command_exists systemctl && [[ "$OS" != "macos" ]]; then
        if confirm_default_yes "  Install as a systemd service (auto-start on boot)?"; then
            SERVICE_FILE="/etc/systemd/system/ponygram.service"
            # Create a service file pointing at this install
            _sudo tee "$SERVICE_FILE" > /dev/null <<SVCEOF
[Unit]
Description=Ponygram Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ponygram

[Install]
WantedBy=multi-user.target
SVCEOF
            _sudo systemctl daemon-reload
            _sudo systemctl enable ponygram
            _sudo systemctl restart ponygram
            SYSTEMD_OK=true
            ok "systemd service installed and started"
            info "Manage: sudo systemctl {status|stop|restart} ponygram"
            info "Logs:   sudo journalctl -u ponygram -f"
        fi
    fi

    if ! $SYSTEMD_OK; then
        echo ""
        info "To start the bot manually:"
        echo -e "    ${BOLD}cd ${INSTALL_DIR} && .venv/bin/python main.py${NC}"
        echo ""
        if confirm "  Start the bot now?"; then
            info "Starting bot (Ctrl+C to stop) …"
            cd "$INSTALL_DIR"
            exec "$INSTALL_DIR/.venv/bin/python" main.py
        fi
    fi
fi

# ── Step 6 : Summary ─────────────────────────────────────────
step "6/6  Done!"
echo ""
echo -e "  ${GREEN}${BOLD}Ponygram is ready!${NC}"
echo ""
echo "  ┌─────────────────────────────────────────────────┐"
echo "  │  Quick-start checklist                          │"
echo "  ├─────────────────────────────────────────────────┤"
echo "  │  1. Add the bot to a Telegram group             │"
echo "  │  2. Make it a group admin                       │"
if [[ "$WEB_ENABLED" == "true" ]]; then
echo "  │  3. Open http://<your-ip>:${WEB_PORT}                │"
echo "  │     Log in and activate the group               │"
else
echo "  │  3. Enable Web UI (WEB_ENABLED=true in .env)    │"
echo "  │     to activate groups and manage settings      │"
fi
echo "  │  4. Paste a YouTube URL → auto-download 🎬      │"
echo "  │  5. Say  pony hello  → AI reply 🤖              │"
echo "  └─────────────────────────────────────────────────┘"
echo ""
echo "  Config file : $ENV_FILE"
echo "  Docs        : $REPO_URL"
echo ""
