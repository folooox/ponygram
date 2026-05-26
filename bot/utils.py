"""
Shared utility helpers.
"""
from __future__ import annotations

import json as _json
import re as _re


def normalize_cookie(raw: str) -> tuple[str, int]:
    """Detect input format and return (header_string, cookie_count).

    Supported input formats:
      1. Cookie-Editor "JSON" export  — JSON array of {name, value, ...} objects
      2. Cookie-Editor "Header String" — key=value;key=value (with/without spaces)
      3. Netscape cookies.txt         — TAB-delimited lines (domain \\t ... \\t name \\t value)

    Always returns a normalised "; "-separated header string ready to be sent
    as the HTTP ``Cookie`` header, e.g. ``sessionid=abc; csrftoken=xyz``.

    Returns ("", 0) if the input is empty or unrecognisable.
    """
    raw = raw.strip()
    if not raw:
        return "", 0

    # ── 1. JSON array ─────────────────────────────────────────────────────────
    if raw.startswith("["):
        try:
            items = _json.loads(raw)
            if isinstance(items, list):
                pairs = [
                    f"{i['name']}={i['value']}"
                    for i in items
                    if isinstance(i, dict) and "name" in i and "value" in i
                ]
                if pairs:
                    return "; ".join(pairs), len(pairs)
        except Exception:
            pass  # fall through to next format

    # ── 2. Netscape cookies.txt (TAB-delimited, ≥7 fields) ────────────────────
    #    Format: domain \t flag \t path \t secure \t expiry \t name \t value
    if "\n" in raw and "\t" in raw:
        pairs = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name, value = parts[5], parts[6]
                if name:
                    pairs.append(f"{name}={value}")
        if pairs:
            return "; ".join(pairs), len(pairs)

    # ── 3. Header String (key=value; key=value …) ─────────────────────────────
    #    Accept both "; " and ";" separators; strip stray whitespace.
    pairs = []
    for part in _re.split(r";\s*", raw):
        part = part.strip()
        if "=" in part:
            pairs.append(part)
    if pairs:
        return "; ".join(pairs), len(pairs)

    return raw, 0  # unrecognised — return as-is so nothing is lost
