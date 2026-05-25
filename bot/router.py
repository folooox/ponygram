"""
Command router and handler registry for ponygram.

Provides:
- ``CommandRegistry`` — a central registry for bot commands and their metadata.
- ``setup_handlers(application)`` — registers all handlers from loaded modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.logger import get_logger

log = get_logger(__name__)


@dataclass
class _CommandEntry:
    command: str
    handler: Callable[..., Coroutine[Any, Any, None]]
    description: str
    admin_only: bool = False


class CommandRegistry:
    """
    Singleton-style registry that tracks every registered bot command.

    Modules call :meth:`register_command` to publish their commands; the
    ``/help`` handler reads :meth:`get_commands_list` to build a dynamic list.
    """

    def __init__(self) -> None:
        self._commands: Dict[str, _CommandEntry] = {}

    # ------------------------------------------------------------------ #
    # Registration API                                                     #
    # ------------------------------------------------------------------ #

    def register_command(
        self,
        command: str,
        handler: Callable[..., Coroutine[Any, Any, None]],
        description: str,
        admin_only: bool = False,
    ) -> None:
        """
        Register a command and its handler.

        Parameters
        ----------
        command:
            Command name **without** the leading slash, e.g. ``"start"``.
        handler:
            Async callable that accepts ``(update, context)``.
        description:
            One-line human-readable description shown in /help output.
        admin_only:
            When True the command is hidden from regular users' /help output.
        """
        command = command.lstrip("/").lower()
        if command in self._commands:
            log.warning("Overwriting already-registered command", command=command)
        self._commands[command] = _CommandEntry(
            command=command,
            handler=handler,
            description=description,
            admin_only=admin_only,
        )
        log.debug("Registered command", command=command, admin_only=admin_only)

    # ------------------------------------------------------------------ #
    # Query API                                                            #
    # ------------------------------------------------------------------ #

    def get_handler(self, command: str) -> Optional[Callable]:
        entry = self._commands.get(command.lstrip("/").lower())
        return entry.handler if entry else None

    def get_commands_list(self, is_admin: bool = False) -> str:
        """
        Build a formatted Markdown command list for /help replies.

        Parameters
        ----------
        is_admin:
            When True, admin-only commands are included in the output.

        Returns
        -------
        str
            A multi-line string of ``/command — description`` pairs.
        """
        lines: List[str] = []
        for entry in self._commands.values():
            if entry.admin_only and not is_admin:
                continue
            suffix = " _(admin)_" if entry.admin_only else ""
            lines.append(f"/{entry.command} — {entry.description}{suffix}")
        return "\n".join(lines) if lines else "No commands registered."

    def get_bot_commands(self, is_admin: bool = False) -> List[BotCommand]:
        """Return a list of :class:`telegram.BotCommand` for set_my_commands."""
        commands = []
        for entry in self._commands.values():
            if entry.admin_only and not is_admin:
                continue
            commands.append(BotCommand(command=entry.command, description=entry.description))
        return commands

    @property
    def all_commands(self) -> Dict[str, _CommandEntry]:
        return dict(self._commands)


# Module-level singleton shared across the application.
registry = CommandRegistry()


def setup_handlers(application: Application) -> None:
    """
    Register all command handlers that have been accumulated in the registry.

    Called by ``main.py`` after all modules and plugins have been loaded.
    Attaches a :class:`~telegram.ext.CommandHandler` for every registered
    command plus a generic fallback for unrecognised text messages.

    Parameters
    ----------
    application:
        The fully-constructed :class:`~telegram.ext.Application` instance.
    """
    for command, entry in registry.all_commands.items():
        application.add_handler(CommandHandler(command, entry.handler))
        log.debug("Handler attached", command=command)

    log.info("All command handlers registered", count=len(registry.all_commands))
