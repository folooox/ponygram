"""Component registry powering the web 组件中心 (component center).

Each feature module describes itself as a :class:`Component`: its editable
output templates, the ``bot_config`` keys it reads, and an optional dry-run
``test`` callable. The web admin reads this registry to render, per component,
three panels: 配置 (config) / 模板编辑 (template editor + preview) / 调试 (debug).

Modules register in their ``setup()`` via :func:`register_component`, mirroring
the existing ``registry.register_command`` pattern in ``bot/router.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from bot import render as _render


@dataclass
class TemplateSpec:
    """One editable output template of a component."""

    key: str                      # stable id, used as output_templates.key
    name: str                     # 中文显示名
    default: str                  # built-in default template
    placeholders: dict[str, str] = field(default_factory=dict)  # name -> 样例值
    help: str = ""                # one-line hint shown in the editor


@dataclass
class ConfigField:
    """One ``bot_config`` setting a component reads, with UI metadata.

    Drives the auto-generated 配置 panel: each field becomes an editable input
    whose control type is decided by ``kind``. ``help`` may contain HTML (e.g. a
    link to where the key is obtained). ``test_service`` reuses the existing
    ``/health/check/{service}`` endpoint for the per-field "测试" button.
    """

    key: str                          # bot_config 键名, e.g. "tmdb_api_key"
    label: str                        # 中文标签
    kind: str = "secret"              # secret | text | int | bool | cookie
    help: str = ""                    # 提示(允许 HTML)
    placeholder: str = ""
    default: str = ""
    test_service: Optional[str] = None  # /health/check/{service} 的 service id
    link: str = ""                    # 外部申请链接(可选)
    link_label: str = ""              # 链接显示文字


# A dry-run test: async (sample_input: str) -> dict. MUST NOT send anything to
# Telegram or mutate state — it only returns a result for display in the web UI.
TestFn = Callable[[str], Awaitable[dict]]


@dataclass
class Component:
    id: str
    name: str                     # 中文名
    icon: str                     # bootstrap-icons class, e.g. "bi-link-45deg"
    description: str
    # Either bare key strings (legacy) or rich ConfigField objects. Read through
    # the ``config_fields`` property which normalizes both into ConfigField.
    config_keys: list = field(default_factory=list)
    templates: list[TemplateSpec] = field(default_factory=list)
    test: Optional[TestFn] = None
    test_label: str = "测试输入"
    test_placeholder: str = ""
    test_sample: str = ""
    order: int = 100              # 组件中心首页排序权重(小在前)
    toggleable: bool = True       # 是否在卡片/详情页提供启停开关

    @property
    def config_fields(self) -> list[ConfigField]:
        """Normalized config metadata; bare strings become secret fields."""
        out: list[ConfigField] = []
        for c in self.config_keys:
            if isinstance(c, ConfigField):
                out.append(c)
            else:
                out.append(ConfigField(key=str(c), label=str(c)))
        return out

    @property
    def config_count(self) -> int:
        """Number of real (editable) config fields, excluding 'link' pseudo-fields."""
        return sum(1 for f in self.config_fields if f.kind != "link")


_REGISTRY: dict[str, Component] = {}


def register_component(comp: Component) -> None:
    """Register a component and seed its template defaults into the render layer."""
    _REGISTRY[comp.id] = comp
    for t in comp.templates:
        _render.register_default(comp.id, t.key, t.default)


def get_component(component_id: str) -> Optional[Component]:
    return _REGISTRY.get(component_id)


def all_components() -> list[Component]:
    """All components, sorted by (order, name) for stable hub display."""
    return sorted(_REGISTRY.values(), key=lambda c: (c.order, c.name))


# --------------------------------------------------------------------------- #
# Per-module enable/disable                                                    #
# --------------------------------------------------------------------------- #
# State lives in bot_config under ``module_enabled_{id}``. Absent or "1" means
# enabled; "0" means disabled. Modules guard their entry points with
# :func:`require_module` (commands) or an inline :func:`is_module_enabled` check
# (message triggers).

async def is_module_enabled(component_id: str) -> bool:
    from bot.database import get_bot_config
    try:
        val = await get_bot_config(f"module_enabled_{component_id}")
    except Exception:
        return True
    return val != "0"


def require_module(component_id: str):
    """Decorator for a PTB handler: drop the update if the module is disabled."""
    from functools import wraps

    def deco(func):
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            if not await is_module_enabled(component_id):
                msg = getattr(update, "effective_message", None)
                if msg is not None:
                    try:
                        await msg.reply_text("⚠️ 该功能已被管理员停用。")
                    except Exception:
                        pass
                return
            return await func(update, context, *args, **kwargs)

        return wrapper

    return deco
