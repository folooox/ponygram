"""Runtime-editable output template rendering.

Output text for each component (its message templates) is stored in the
``output_templates`` table keyed by ``(component_id, key)``. When no custom
template is saved, we fall back to the DEFAULT registered here by the owning
module. Templates are read at call time (per CLAUDE.md convention) so edits in
the web 组件中心 take effect immediately without a restart.

Rendering uses ``str.format_map`` with a forgiving dict so a missing or
mistyped placeholder in a user-edited template never raises at runtime — it is
left as a literal ``{placeholder}`` instead.
"""
from __future__ import annotations

from typing import Any

from bot.database import get_template

# component_id -> {key: default template string}
_DEFAULTS: dict[str, dict[str, str]] = {}


class _SafeDict(dict):
    """dict that leaves unknown placeholders untouched instead of raising."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return "{" + key + "}"


def register_default(component_id: str, key: str, template: str) -> None:
    """Register the built-in default template for a component output key."""
    _DEFAULTS.setdefault(component_id, {})[key] = template


def get_default(component_id: str, key: str) -> str:
    return _DEFAULTS.get(component_id, {}).get(key, "")


def render_str(template: str, **vars: Any) -> str:
    """Render a template string with the given variables (forgiving)."""
    safe = _SafeDict({k: ("" if v is None else v) for k, v in vars.items()})
    try:
        return template.format_map(safe)
    except Exception:
        # Malformed template (e.g. stray brace) — return as-is rather than crash.
        return template


async def render(component_id: str, key: str, **vars: Any) -> str:
    """Render component output: custom DB template if present, else default."""
    tmpl = await get_template(component_id, key)
    if tmpl is None:
        tmpl = get_default(component_id, key)
    return render_str(tmpl, **vars)
