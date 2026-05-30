"""
Centralized AI (DeepSeek) client with usage logging.

All modules should call deepseek_call() instead of making raw aiohttp
requests so usage is tracked in ai_usage_log and visible in the web panel.
"""
from __future__ import annotations

import aiohttp

from bot.logger import get_logger

log = get_logger(__name__)


async def deepseek_call(
    module: str,
    operation: str,
    messages: list[dict],
    max_tokens: int = 400,
    temperature: float = 0,
) -> str | None:
    """
    Call DeepSeek chat completions. Returns the response text, or None on
    failure. The caller should degrade gracefully when None is returned.

    Args:
        module: Which bot module is calling (e.g. "psn", "article").
        operation: What it's doing (e.g. "translate", "summarize").
        messages: OpenAI-style message list.
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
    """
    from bot.database import get_bot_config, log_ai_usage

    key = await get_bot_config("deepseek_api_key")
    if not key:
        return None

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    log.warning("DeepSeek non-200", module=module, operation=operation,
                                status=r.status)
                    await log_ai_usage(module, operation, "deepseek-chat", success=False)
                    return None
                data = await r.json()

        usage = data.get("usage", {})
        text = data["choices"][0]["message"]["content"].strip()
        await log_ai_usage(
            module, operation, "deepseek-chat",
            prompt_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            success=True,
        )
        return text

    except Exception as e:
        log.warning("DeepSeek call failed", module=module, operation=operation, error=str(e))
        await log_ai_usage(module, operation, "deepseek-chat", success=False)
        return None
