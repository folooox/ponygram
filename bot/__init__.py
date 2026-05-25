"""
ponygram bot package.

Re-exports the primary public surface so callers can do:
    from bot import load_config, setup_logging, get_logger, setup_handlers
"""

from bot.cache import TTLCache, movie_cache, book_cache, music_cache
from bot.config import Config, load_config
from bot.database import init_db
from bot.logger import setup_logging, get_logger
from bot.permissions import admin_only, owner_only, group_only, not_blacklisted
from bot.router import CommandRegistry, setup_handlers
from bot.error_handler import error_handler
from bot.plugin_loader import PluginLoader

__all__ = [
    "TTLCache",
    "movie_cache",
    "book_cache",
    "music_cache",
    "Config",
    "load_config",
    "init_db",
    "setup_logging",
    "get_logger",
    "admin_only",
    "owner_only",
    "group_only",
    "not_blacklisted",
    "CommandRegistry",
    "setup_handlers",
    "error_handler",
    "PluginLoader",
]
