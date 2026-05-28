#!/usr/bin/env python3
"""
Command-line script to import PS Store export CSV into psn_library_games.

Usage:
    python scripts/import_ps_plus_csv.py --csv /path/to/ps_plus_games.csv [--clear]

The CSV must be in PS Store export format (GBK encoding):
    name, service_tier, platforms, product_id, product_url, image_url, is_active, first_seen_at

Options:
    --csv PATH      Path to the GBK-encoded CSV file
    --clear         Clear all existing psn_library_games rows before import
    --no-ai         Skip AI translation for pure-Chinese titles
    --env PATH      Path to .env file (default: .env in project root)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env(env_path: str) -> None:
    p = Path(env_path)
    if not p.exists():
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_SKIP_EN = re.compile(r'^\s*[-–]|PS[45]|PlayStation|^&\s*PS[45]', re.IGNORECASE)


def _extract_en_from_name(name: str):
    """Extract (title_en, title_zh) from PS Store name field."""
    name = name.strip()
    m = re.search(r'《([^》]+)》', name)
    if m:
        inner = m.group(1).strip()
        after_raw = name[m.end():].strip()
        after_pre = re.split(r'[\(（]', after_raw)[0].strip().rstrip('–- \t')

        if re.search(r'[一-鿿぀-ヿ가-힯]', inner):
            zh = inner
            if (after_pre and len(after_pre) >= 3
                    and not re.search(r'[一-鿿぀-ヿ가-힯]', after_pre)
                    and re.search(r'[A-Za-z]', after_pre)
                    and not _SKIP_EN.search(after_pre)):
                return after_pre, zh
            return "", zh
        else:
            return inner, ""

    if not re.search(r'[一-鿿぀-ヿ가-힯]', name):
        en = re.split(r'[\(（]', name)[0].strip()
        en = re.sub(r'\s*[-–]\s*(PS[45]|PlayStation[45]?|中文版|繁體|簡體|英文|日文).*$',
                    '', en, flags=re.IGNORECASE).strip()
        return en, ""
    return "", name


def _parse_csv(data: bytes):
    for enc in ("gbk", "utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")

    tier_map = {"ESSENTIAL": 1, "EXTRA": 2, "PREMIUM": 3}
    rows = []
    needs_translation: List[tuple] = []

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        raw_name = (row.get("name") or "").strip()
        if not raw_name:
            continue

        title_en, title_zh = _extract_en_from_name(raw_name)
        tier_raw = (row.get("service_tier") or "").strip().upper()
        tier = tier_map.get(tier_raw, 2)

        plat_raw = (row.get("platforms") or "").strip()
        try:
            plat_list = json.loads(plat_raw)
            platforms = ",".join(plat_list) if isinstance(plat_list, list) else plat_raw
        except Exception:
            platforms = plat_raw.replace("|", ",")

        first_seen = (row.get("first_seen_at") or "").strip()
        entry_date = None
        for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                entry_date = datetime.strptime(first_seen, fmt).date().isoformat()
                break
            except ValueError:
                continue

        status = "active" if (row.get("is_active") or "").strip() == "1" else "removed"

        idx = len(rows)
        rows.append({
            "title_en": title_en,
            "title_zh": title_zh,
            "tier": str(tier),
            "product_id": (row.get("product_id") or "").strip(),
            "store_url": (row.get("product_url") or "").strip(),
            "cover_url": (row.get("image_url") or "").strip(),
            "platforms": platforms,
            "entry_date": entry_date or "",
            "status": status,
            "region": "HK",
        })
        if not title_en and title_zh:
            needs_translation.append((idx, title_zh))

    return rows, needs_translation


async def _ai_translate_batch(batch: List[tuple], rows: List[dict]) -> int:
    """Translate pure-Chinese titles via DeepSeek. Returns count translated."""
    import aiohttp

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not deepseek_key:
        # Try reading from DB
        from bot.database import get_bot_config
        deepseek_key = (await get_bot_config("deepseek_api_key")) or ""

    if not deepseek_key:
        print(f"  [skip] No DeepSeek API key — skipping AI translation of {len(batch)} titles")
        return 0

    names_list = "\n".join(f'{i+1}. {zh}' for i, (_, zh) in enumerate(batch))
    prompt = (
        f"Translate these {len(batch)} Chinese video game titles to English. "
        f"Reply with ONLY a JSON array of strings in the same order:\n{names_list}"
    )

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {deepseek_key}",
                         "Content-Type": "application/json"},
                json={"model": "deepseek-chat",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 400, "temperature": 0},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    print(f"  [warn] DeepSeek returned {r.status}")
                    return 0
                resp = await r.json()
                text = resp["choices"][0]["message"]["content"].strip()
                text = re.sub(r"```[a-z]*\n?", "", text).strip("` \n")
                translated = json.loads(text)
    except Exception as e:
        print(f"  [warn] AI translation failed: {e}")
        return 0

    if not isinstance(translated, list) or len(translated) != len(batch):
        print(f"  [warn] Unexpected translation response shape")
        return 0

    count = 0
    for j, (row_idx, _) in enumerate(batch):
        en = str(translated[j]).strip()
        if en:
            rows[row_idx]["title_en"] = en
            count += 1
    return count


async def main(csv_path: str, clear: bool, no_ai: bool) -> None:
    _load_env(str(ROOT / ".env"))

    from bot.config import load_config
    from bot.database import init_db, bulk_import_psn_games, get_session

    cfg = load_config()
    await init_db(cfg.database_url)

    data = Path(csv_path).read_bytes()
    rows, needs_translation = _parse_csv(data)
    print(f"Parsed {len(rows)} rows, {len(needs_translation)} need AI translation")

    ai_translated = 0
    if needs_translation and not no_ai:
        print(f"Translating {len(needs_translation)} Chinese titles via DeepSeek…")
        batch_size = 20
        for i in range(0, len(needs_translation), batch_size):
            batch = needs_translation[i:i + batch_size]
            n = await _ai_translate_batch(batch, rows)
            ai_translated += n
            print(f"  batch {i//batch_size + 1}: {n}/{len(batch)} translated")
        print(f"AI translated: {ai_translated} titles")

    if clear:
        async with get_session() as s:
            from sqlalchemy import delete
            from bot.database import PsnLibraryGame
            await s.execute(delete(PsnLibraryGame))
            await s.commit()
        print("Cleared existing psn_library_games")

    ok, err = await bulk_import_psn_games(rows)
    print(f"Import complete: {ok} succeeded, {err} failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import PS Store CSV into psn_library_games")
    parser.add_argument("--csv", required=True, help="Path to PS Store export CSV (GBK encoded)")
    parser.add_argument("--clear", action="store_true", help="Clear DB before import")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI translation")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="Path to .env file")
    args = parser.parse_args()

    _load_env(args.env)
    asyncio.run(main(args.csv, args.clear, args.no_ai))
