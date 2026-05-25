"""
Dynamic loader for built-in modules and third-party plugins.

Convention
----------
Every module / plugin file must expose a top-level async (or sync) function::

    async def setup(application: Application) -> None:
        ...

The loader calls this function after importing the module so each module can
register its own handlers, jobs, and commands.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import List

from telegram.ext import Application

from bot.logger import get_logger

log = get_logger(__name__)


class PluginLoader:
    """
    Discovers and loads bot modules from a directory.

    Two entry points are provided:

    - :meth:`load_modules` — for built-in modules shipped with ponygram.
    - :meth:`load_plugins` — for optional third-party plugins placed in the
      ``plugins/`` directory.

    Both use identical logic; the distinction is purely organisational.
    """

    def __init__(self) -> None:
        self._loaded: List[str] = []
        self._failed: List[str] = []

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def load_modules(self, app: Application, modules_dir: str) -> None:
        """
        Load all ``.py`` files from *modules_dir* as built-in modules.

        Parameters
        ----------
        app:
            The running Application instance passed to each module's setup().
        modules_dir:
            Filesystem path to the ``modules/`` directory.
        """
        await self._load_directory(app, Path(modules_dir), package_prefix="modules")

    async def load_plugins(self, app: Application, plugins_dir: str) -> None:
        """
        Load all ``.py`` files from *plugins_dir* as optional plugins.

        Parameters
        ----------
        app:
            The running Application instance passed to each plugin's setup().
        plugins_dir:
            Filesystem path to the ``plugins/`` directory.
        """
        await self._load_directory(app, Path(plugins_dir), package_prefix="plugins")

    @property
    def loaded(self) -> List[str]:
        """Names of successfully loaded modules/plugins."""
        return list(self._loaded)

    @property
    def failed(self) -> List[str]:
        """Names of modules/plugins that failed to load."""
        return list(self._failed)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _load_directory(
        self, app: Application, directory: Path, package_prefix: str
    ) -> None:
        if not directory.exists():
            log.warning("Directory does not exist, skipping", path=str(directory))
            return

        py_files = sorted(
            f for f in directory.glob("*.py") if f.name != "__init__.py"
        )

        if not py_files:
            log.info("No modules found in directory", path=str(directory))
            return

        for py_file in py_files:
            module_name = f"{package_prefix}.{py_file.stem}"
            await self._load_single(app, py_file, module_name)

        log.info(
            "Module loading complete",
            directory=str(directory),
            loaded=len(self._loaded),
            failed=len(self._failed),
        )

    async def _load_single(
        self, app: Application, path: Path, module_name: str
    ) -> None:
        """Import a single file as *module_name* and call its setup()."""
        try:
            # Use importlib.util so we can load files that are not on sys.path.
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {path}")

            module: ModuleType = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[attr-defined]

            setup_fn = getattr(module, "setup", None)
            if setup_fn is None:
                log.warning(
                    "Module has no setup() function — skipping",
                    module=module_name,
                    path=str(path),
                )
                self._failed.append(module_name)
                return

            # Support both sync and async setup functions.
            import asyncio, inspect
            if inspect.iscoroutinefunction(setup_fn):
                await setup_fn(app)
            else:
                setup_fn(app)

            self._loaded.append(module_name)
            log.info("Loaded module", module=module_name)

        except Exception as exc:  # noqa: BLE001
            self._failed.append(module_name)
            log.error(
                "Failed to load module",
                module=module_name,
                path=str(path),
                error=str(exc),
                exc_info=True,
            )
