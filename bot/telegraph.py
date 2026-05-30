"""
Telegraph utilities shared across media and article modules.

Provides helpers to create/manage a Telegraph account token and
convert markdown-like content to Telegraph page nodes.
"""
from __future__ import annotations

import re
from typing import Optional

import aiohttp

_TELEGRAPH_TOKEN: Optional[str] = None

_INLINE_MD = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|_(.+?)_|`(.+?)`", re.DOTALL)


def _parse_inline_md(text: str) -> list:
    """Convert inline markdown in text to Telegraph children node list."""
    children: list = []
    last = 0
    for m in _INLINE_MD.finditer(text):
        if m.start() > last:
            children.append(text[last:m.start()])
        if m.group(1) is not None:
            children.append({"tag": "b", "children": [m.group(1)]})
        elif m.group(2) is not None:
            children.append({"tag": "i", "children": [m.group(2)]})
        elif m.group(3) is not None:
            children.append({"tag": "i", "children": [m.group(3)]})
        else:
            children.append({"tag": "code", "children": [m.group(4)]})
        last = m.end()
    if last < len(text):
        children.append(text[last:])
    return children or [text]


def _first_paragraph(md: str, max_len: int = 200) -> str:
    """Extract first non-header paragraph from markdown as plain text."""
    for block in re.split(r'\n{2,}', md):
        block = block.strip()
        if not block or block.startswith('#'):
            continue
        block = re.sub(r'!\[.*?\]\(.*?\)', '', block)
        block = re.sub(r'\[(.+?)\]\(.*?\)', r'\1', block)
        block = re.sub(r'\*\*(.+?)\*\*', r'\1', block)
        block = re.sub(r'\*(.+?)\*', r'\1', block)
        block = re.sub(r'`(.+?)`', r'\1', block)
        block = block.strip()
        if block:
            return block[:max_len]
    return ""


def _md_to_telegraph_nodes(md: str) -> list:
    nodes = []
    for line in md.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("# "):
            nodes.append({"tag": "h3", "children": _parse_inline_md(line[2:])})
        elif line.startswith(("## ", "### ")):
            nodes.append({"tag": "h4", "children": _parse_inline_md(line.lstrip("#").strip())})
        elif m := re.match(r"!\[.*?\]\((https?://\S+)\)", line):
            nodes.append({"tag": "figure", "children": [
                {"tag": "img", "attrs": {"src": m.group(1)}}
            ]})
        else:
            nodes.append({"tag": "p", "children": _parse_inline_md(line)})
    return nodes or [{"tag": "p", "children": [" "]}]


async def _ensure_telegraph_token() -> Optional[str]:
    global _TELEGRAPH_TOKEN
    if _TELEGRAPH_TOKEN:
        return _TELEGRAPH_TOKEN
    from bot.database import get_bot_config, set_bot_config
    token = await get_bot_config("telegraph_token")
    if not token:
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.post(
                    "https://api.telegra.ph/createAccount",
                    json={"short_name": "PonygramBot", "author_name": "Ponygram"},
                )
                d = await r.json()
                token = d.get("result", {}).get("access_token")
                if token:
                    await set_bot_config("telegraph_token", token)
        except Exception:
            return None
    _TELEGRAPH_TOKEN = token
    return token


async def _post_to_telegraph(title: str, nodes: list, token: str) -> str:
    async with aiohttp.ClientSession() as s:
        r = await s.post(
            "https://api.telegra.ph/createPage",
            json={
                "access_token": token,
                "title": title[:256] or "Article",
                "content": nodes,
                "return_content": False,
            },
        )
        d = await r.json()
        return d["result"]["url"]
