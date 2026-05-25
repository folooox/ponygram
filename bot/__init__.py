"""
ponygram bot package.

Re-exports the primary public surface so callers can do:
    from bot import load_config, setup_logging, get_logger, setup_handlers
"""

from bot.config import Config, load_config
from bot.logger import setup_logging, get_logger
from bot.router import CommandRegistry, setup_handlers
from bot.error_handler import error_handler
from bot.plugin_loader import PluginLoader

__all__ = [
    "Config",
    "load_config",
    "setup_logging",
    "get_logger",
    "CommandRegistry",
    "setup_handlers",
    "error_handler",
    "PluginLoader",
]
