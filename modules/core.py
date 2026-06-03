"""Core / global component.

Holds cross-cutting ``bot_config`` keys that are shared by several modules
rather than owned by any single one — currently the DeepSeek API key used for
translation/summarization across article, psn, etc. Registering it as a
Component makes it appear and be editable in the web 组件中心 like every other
module, instead of living in a hardcoded settings list.
"""
from __future__ import annotations

from telegram.ext import Application

from bot.components import Component, ConfigField, register_component
from bot.logger import get_logger

log = get_logger(__name__)

_COMPONENT = Component(
    id="core",
    name="全局 AI",
    icon="bi-cpu",
    description="跨模块共享的 AI 配置。DeepSeek 用于游戏名翻译、文章摘要/标题翻译等，优先于 Claude。",
    config_keys=[
        ConfigField(
            key="deepseek_api_key",
            label="DeepSeek API Key",
            kind="secret",
            help='前往 platform.deepseek.com → API Keys → 创建新密钥。用于全局翻译/摘要，成本低、速度快。',
            placeholder="sk-…",
            test_service="deepseek",
            link="https://platform.deepseek.com/api_keys",
            link_label="platform.deepseek.com",
        ),
    ],
    order=0,
    toggleable=False,
)


def setup(application: Application) -> None:
    register_component(_COMPONENT)
    log.info("core component loaded")
