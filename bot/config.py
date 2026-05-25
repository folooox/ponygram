"""
Configuration management for ponygram.

Reads all settings from environment variables (populated via python-dotenv).
No third-party schema libraries — plain dataclasses + manual validation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv


@dataclass
class Config:
    """Holds all runtime configuration for the bot."""

    # Core
    bot_token: str
    owner_id: int
    admin_ids: List[int]

    # Database
    database_url: str

    # Logging
    log_level: str
    log_file: str

    # Webhook (optional — empty string means polling)
    webhook_url: str
    webhook_port: int

    # Optional external API keys (may be empty strings)
    tmdb_api_key: str
    lastfm_api_key: str
    claude_api_key: str

    # Web admin UI
    web_enabled: bool
    web_host: str
    web_port: int
    web_secret: str

    def is_owner(self, user_id: int) -> bool:
        """Return True if *user_id* is the bot owner."""
        return user_id == self.owner_id

    def is_admin(self, user_id: int) -> bool:
        """Return True if *user_id* is an admin **or** the owner."""
        return user_id == self.owner_id or user_id in self.admin_ids

    @property
    def use_webhook(self) -> bool:
        """Return True when a webhook URL has been configured."""
        return bool(self.webhook_url)


def load_config(env_file: Optional[str] = None) -> Config:
    """
    Load configuration from the environment (and optionally a .env file).

    Parameters
    ----------
    env_file:
        Path to a .env file.  When *None* the function tries '.env' in the
        current working directory (standard python-dotenv behaviour).

    Returns
    -------
    Config
        Populated and validated configuration object.

    Raises
    ------
    ValueError
        When a required variable (BOT_TOKEN) is missing or invalid.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        load_dotenv(override=False)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError(
            "BOT_TOKEN is not set.  Copy .env.example to .env and fill in your token."
        )

    # Owner ID
    raw_owner = os.getenv("OWNER_ID", "0").strip()
    try:
        owner_id = int(raw_owner)
    except ValueError:
        raise ValueError(f"OWNER_ID must be an integer, got: {raw_owner!r}")

    # Admin IDs (comma-separated list, tolerant of whitespace / empty values)
    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: List[int] = []
    if raw_admins:
        for part in raw_admins.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                admin_ids.append(int(part))
            except ValueError:
                raise ValueError(
                    f"ADMIN_IDS contains a non-integer value: {part!r}"
                )

    database_url = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///data/ponygram.db"
    ).strip()

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        raise ValueError(
            f"LOG_LEVEL must be one of {valid_levels}, got: {log_level!r}"
        )

    log_file = os.getenv("LOG_FILE", "logs/ponygram.log").strip()

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()

    raw_port = os.getenv("WEBHOOK_PORT", "8443").strip()
    try:
        webhook_port = int(raw_port) if raw_port else 8443
    except ValueError:
        raise ValueError(f"WEBHOOK_PORT must be an integer, got: {raw_port!r}")

    web_enabled = os.getenv("WEB_ENABLED", "false").lower() in ("true", "1", "yes")
    web_host = os.getenv("WEB_HOST", "0.0.0.0").strip()
    raw_web_port = os.getenv("WEB_PORT", "8080").strip()
    try:
        web_port = int(raw_web_port)
    except ValueError:
        raise ValueError(f"WEB_PORT must be an integer, got: {raw_web_port!r}")
    web_secret = os.getenv("WEB_SECRET", "").strip()

    return Config(
        bot_token=bot_token,
        owner_id=owner_id,
        admin_ids=admin_ids,
        database_url=database_url,
        log_level=log_level,
        log_file=log_file,
        webhook_url=webhook_url,
        webhook_port=webhook_port,
        tmdb_api_key=os.getenv("TMDB_API_KEY", "").strip(),
        lastfm_api_key=os.getenv("LASTFM_API_KEY", "").strip(),
        claude_api_key=os.getenv("CLAUDE_API_KEY", "").strip(),
        web_enabled=web_enabled,
        web_host=web_host,
        web_port=web_port,
        web_secret=web_secret,
    )
