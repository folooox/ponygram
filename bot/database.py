"""
Async database layer — SQLite via SQLAlchemy 2 + aiosqlite.

Tables
------
users               — every user seen by the bot
group_settings      — per-group configuration (welcome text, verification, etc.)
blacklist           — banned user IDs (global)
rss_feeds           — per-chat RSS subscriptions
rss_sent            — deduplication log of pushed entries
bot_config          — key-value store for bot-level settings (API keys, etc.)
psn_library_games   — PS Plus game library (tier 1=Essential, 2=Extra, 3=Premium)
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    func,
    or_,
    select,
    delete,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from bot.logger import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)          # Telegram user ID
    username = Column(String(64), nullable=True)
    first_name = Column(String(128), nullable=False, default="")
    last_name = Column(String(128), nullable=True)
    is_bot = Column(Boolean, default=False)
    language_code = Column(String(16), nullable=True)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GroupSettings(Base):
    __tablename__ = "group_settings"

    chat_id = Column(BigInteger, primary_key=True)
    is_active = Column(Boolean, default=False)         # must be activated by owner in Web UI
    welcome_enabled = Column(Boolean, default=True)
    welcome_text = Column(Text, nullable=True)         # None → use default
    goodbye_enabled = Column(Boolean, default=False)
    goodbye_text = Column(Text, nullable=True)
    verification_enabled = Column(Boolean, default=False)
    verification_timeout = Column(Integer, default=60) # seconds
    antispam_enabled = Column(Boolean, default=True)
    antispam_max_msgs = Column(Integer, default=5)     # msgs per window
    antispam_window = Column(Integer, default=5)       # seconds
    antiad_enabled = Column(Boolean, default=False)
    warn_limit = Column(Integer, default=3)
    dlmode_enabled = Column(Boolean, default=True)     # auto media URL detection (on by default)
    aichat_enabled = Column(Boolean, default=True)     # AI auto-reply (on by default)
    rules_text = Column(Text, nullable=True)           # group rules displayed by /rules command


class Blacklist(Base):
    __tablename__ = "blacklist"

    user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    added_by = Column(BigInteger, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class UserWarn(Base):
    __tablename__ = "user_warns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    reason = Column(Text, nullable=True)
    warned_by = Column(BigInteger, nullable=True)
    warned_at = Column(DateTime, default=datetime.utcnow)


class RssFeed(Base):
    __tablename__ = "rss_feeds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    url = Column(Text, nullable=False)
    label = Column(String(128), nullable=True)   # human-friendly name
    added_by = Column(BigInteger, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    paused = Column(Boolean, default=False)
    last_fetched = Column(DateTime, nullable=True)


class RssSent(Base):
    """Deduplication: tracks entry GUIDs/links already pushed per feed."""
    __tablename__ = "rss_sent"

    id = Column(Integer, primary_key=True, autoincrement=True)
    feed_id = Column(Integer, nullable=False, index=True)
    entry_hash = Column(String(64), nullable=False)   # sha256 of guid or link
    sent_at = Column(DateTime, default=datetime.utcnow)


class BotConfig(Base):
    """Global bot configuration stored as key-value pairs."""
    __tablename__ = "bot_config"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PlatformCredential(Base):
    """Encrypted login credentials for Playwright cookie auto-refresh."""
    __tablename__ = "platform_credentials"

    platform = Column(String(32), primary_key=True)   # e.g. "instagram"
    username = Column(Text, nullable=False)
    password_enc = Column(Text, nullable=False)        # Fernet-encrypted with bot token
    last_refresh_at = Column(DateTime, nullable=True)
    last_refresh_ok = Column(Boolean, nullable=True)   # None=never tried


class PsnLibraryGame(Base):
    """PS Plus games: tier=1 Essential(会免), tier=2 Extra, tier=3 Premium."""
    __tablename__ = "psn_library_games"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    title_en     = Column(Text, nullable=False)
    title_zh     = Column(Text, nullable=True)
    concept_id   = Column(BigInteger, nullable=True, index=True)
    product_id   = Column(Text, nullable=True)
    store_url    = Column(Text, nullable=True)
    cover_url    = Column(Text, nullable=True)
    release_date = Column(Date, nullable=True)
    entry_date   = Column(Date, nullable=False)
    exit_date    = Column(Date, nullable=True)
    tier         = Column(Integer, nullable=False, index=True)  # 1=Essential 2=Extra 3=Premium
    status       = Column(String(16), default="active")         # active / removed
    platforms    = Column(Text, nullable=True)                   # "PS4,PS5"
    genre        = Column(Text, nullable=True)
    age_rating   = Column(Text, nullable=True)
    streaming    = Column(Boolean, default=False)
    region       = Column(String(8), default="HK")
    note         = Column(Text, nullable=True)
    extra_data   = Column(Text, nullable=True, default="{}")     # user-defined JSON fields
    created_at   = Column(DateTime, default=datetime.utcnow)


class PsnColumnsConfig(Base):
    """User-configurable column definitions for the PS Library web UI."""
    __tablename__ = "psn_columns_config"

    col_key      = Column(String(64), primary_key=True)
    display_name = Column(Text, nullable=False)
    col_type     = Column(String(16), default="text")   # text|number|date|url|badge|image
    is_core      = Column(Boolean, default=False)        # True = fixed DB column
    visible      = Column(Boolean, default=True)
    sort_order   = Column(Integer, default=100)


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------

_engine = None
_session_factory: Optional[async_sessionmaker] = None


async def _migrate(conn) -> None:
    """Add missing columns to existing tables (SQLite ALTER TABLE)."""
    from sqlalchemy import text

    # Columns added after v1 — (table, column, definition)
    migrations = [
        ("group_settings",    "is_active",     "BOOLEAN NOT NULL DEFAULT 0"),
        ("group_settings",    "welcome_text",  "TEXT"),
        ("group_settings",    "goodbye_text",  "TEXT"),
        ("group_settings",    "dlmode_enabled","BOOLEAN NOT NULL DEFAULT 1"),
        ("group_settings",    "aichat_enabled","BOOLEAN NOT NULL DEFAULT 1"),
        ("group_settings",    "rules_text",    "TEXT"),
        ("psn_library_games", "extra_data",    "TEXT DEFAULT '{}'"),
    ]

    for table, col, definition in migrations:
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
        existing = {r[1] for r in rows}
        if col not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))
            log.info("DB migration: added column", table=table, column=col)


_DEFAULT_COLUMNS = [
    ("cover_url",    "封面",           "image",  True,   True,   5),
    ("title_zh",     "中文名",          "text",   True,   True,   10),
    ("title_en",     "英文名",          "text",   True,   True,   20),
    ("tier",         "档位",           "badge",  True,   True,   30),
    ("platforms",    "平台",           "text",   True,   True,   40),
    ("status",       "状态",           "badge",  True,   True,   50),
    ("entry_date",   "入库日期",        "date",   True,   True,   60),
    ("exit_date",    "出库日期",        "date",   True,   False,  70),
    ("store_url",    "PS Store 链接",   "url",    True,   True,   80),
    ("product_id",   "Product ID",     "text",   True,   False,  90),
    ("note",         "备注",           "text",   True,   True,   100),
]


async def _seed_default_columns() -> None:
    """Insert default column definitions on first run (skips if already exist)."""
    async with get_session() as s:
        existing = await s.execute(select(PsnColumnsConfig))
        if existing.scalars().first():
            return
        for col_key, display_name, col_type, is_core, visible, sort_order in _DEFAULT_COLUMNS:
            s.add(PsnColumnsConfig(
                col_key=col_key,
                display_name=display_name,
                col_type=col_type,
                is_core=is_core,
                visible=visible,
                sort_order=sort_order,
            ))
        await s.commit()


async def init_db(database_url: str) -> None:
    """Create engine, session factory, and all tables."""
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)
    await _seed_default_columns()
    log.info("Database initialised", url=database_url)


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _session_factory()


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

async def upsert_user(tg_user) -> None:
    """Insert or update a Telegram User record."""
    async with get_session() as s:
        existing = await s.get(User, tg_user.id)
        if existing:
            existing.username = tg_user.username
            existing.first_name = tg_user.first_name or ""
            existing.last_name = tg_user.last_name
            existing.last_seen = datetime.utcnow()
        else:
            s.add(User(
                id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name or "",
                last_name=tg_user.last_name,
                is_bot=tg_user.is_bot,
                language_code=getattr(tg_user, "language_code", None),
            ))
        await s.commit()


async def get_user(user_id: int) -> Optional[User]:
    async with get_session() as s:
        return await s.get(User, user_id)


# ---------------------------------------------------------------------------
# Blacklist helpers
# ---------------------------------------------------------------------------

async def add_to_blacklist(user_id: int, reason: str = "", added_by: int = 0) -> None:
    async with get_session() as s:
        existing = await s.get(Blacklist, user_id)
        if not existing:
            s.add(Blacklist(user_id=user_id, reason=reason, added_by=added_by))
            await s.commit()


async def remove_from_blacklist(user_id: int) -> bool:
    async with get_session() as s:
        row = await s.get(Blacklist, user_id)
        if row:
            await s.delete(row)
            await s.commit()
            return True
        return False


async def is_blacklisted(user_id: int) -> bool:
    async with get_session() as s:
        return await s.get(Blacklist, user_id) is not None


# ---------------------------------------------------------------------------
# Group settings helpers
# ---------------------------------------------------------------------------

async def get_group_settings(chat_id: int) -> GroupSettings:
    async with get_session() as s:
        row = await s.get(GroupSettings, chat_id)
        if not row:
            row = GroupSettings(chat_id=chat_id)
            s.add(row)
            await s.commit()
        return row


async def set_group_field(chat_id: int, **kwargs: Any) -> None:
    async with get_session() as s:
        row = await s.get(GroupSettings, chat_id)
        if not row:
            row = GroupSettings(chat_id=chat_id, **kwargs)
            s.add(row)
        else:
            for k, v in kwargs.items():
                setattr(row, k, v)
        await s.commit()


# ---------------------------------------------------------------------------
# RSS helpers
# ---------------------------------------------------------------------------

async def add_rss_feed(chat_id: int, url: str, label: str = "", added_by: int = 0) -> RssFeed:
    async with get_session() as s:
        feed = RssFeed(chat_id=chat_id, url=url, label=label or None, added_by=added_by)
        s.add(feed)
        await s.commit()
        return feed


async def remove_rss_feed(feed_id: int, chat_id: int) -> bool:
    """Delete a feed only if it belongs to chat_id. Returns True if deleted."""
    async with get_session() as s:
        row = await s.get(RssFeed, feed_id)
        if not row or row.chat_id != chat_id:
            return False
        # Delete sent history for this feed too
        await s.execute(delete(RssSent).where(RssSent.feed_id == feed_id))
        await s.delete(row)
        await s.commit()
        return True


async def get_rss_feeds(chat_id: int) -> List[RssFeed]:
    async with get_session() as s:
        result = await s.execute(
            select(RssFeed).where(RssFeed.chat_id == chat_id).order_by(RssFeed.id)
        )
        return list(result.scalars().all())


async def get_all_active_feeds() -> List[RssFeed]:
    async with get_session() as s:
        result = await s.execute(
            select(RssFeed).where(RssFeed.paused == False)  # noqa: E712
        )
        return list(result.scalars().all())


async def set_feed_paused(feed_id: int, chat_id: int, paused: bool) -> bool:
    async with get_session() as s:
        row = await s.get(RssFeed, feed_id)
        if not row or row.chat_id != chat_id:
            return False
        row.paused = paused
        await s.commit()
        return True


async def mark_feed_fetched(feed_id: int) -> None:
    async with get_session() as s:
        row = await s.get(RssFeed, feed_id)
        if row:
            row.last_fetched = datetime.utcnow()
            await s.commit()


def _entry_hash(entry) -> str:
    key = getattr(entry, "id", None) or getattr(entry, "link", "") or entry.get("title", "")
    return hashlib.sha256(key.encode()).hexdigest()


async def is_entry_sent(feed_id: int, entry) -> bool:
    h = _entry_hash(entry)
    async with get_session() as s:
        result = await s.execute(
            select(RssSent).where(RssSent.feed_id == feed_id, RssSent.entry_hash == h)
        )
        return result.scalar() is not None


async def mark_entry_sent(feed_id: int, entry) -> None:
    h = _entry_hash(entry)
    async with get_session() as s:
        s.add(RssSent(feed_id=feed_id, entry_hash=h))
        await s.commit()


async def prune_sent_history(feed_id: int, keep: int = 500) -> None:
    """Keep only the most recent `keep` sent entries to prevent unbounded growth."""
    async with get_session() as s:
        result = await s.execute(
            select(RssSent.id)
            .where(RssSent.feed_id == feed_id)
            .order_by(RssSent.id.desc())
            .offset(keep)
        )
        old_ids = [row[0] for row in result.all()]
        if old_ids:
            await s.execute(delete(RssSent).where(RssSent.id.in_(old_ids)))
            await s.commit()


# ---------------------------------------------------------------------------
# Warn helpers
# ---------------------------------------------------------------------------

async def add_warn(chat_id: int, user_id: int, reason: str = "", warned_by: int = 0) -> int:
    """Add a warning and return the new total count for this user in this chat."""
    async with get_session() as s:
        s.add(UserWarn(chat_id=chat_id, user_id=user_id, reason=reason or None, warned_by=warned_by))
        await s.commit()
    return await get_warn_count(chat_id, user_id)


async def get_warn_count(chat_id: int, user_id: int) -> int:
    async with get_session() as s:
        result = await s.execute(
            select(UserWarn).where(UserWarn.chat_id == chat_id, UserWarn.user_id == user_id)
        )
        return len(result.scalars().all())


async def get_warns(chat_id: int, user_id: int) -> List[UserWarn]:
    async with get_session() as s:
        result = await s.execute(
            select(UserWarn)
            .where(UserWarn.chat_id == chat_id, UserWarn.user_id == user_id)
            .order_by(UserWarn.warned_at)
        )
        return list(result.scalars().all())


async def clear_warns(chat_id: int, user_id: int) -> int:
    """Remove all warnings for user in chat. Returns count removed."""
    async with get_session() as s:
        result = await s.execute(
            delete(UserWarn).where(UserWarn.chat_id == chat_id, UserWarn.user_id == user_id)
        )
        await s.commit()
        return result.rowcount


# ---------------------------------------------------------------------------
# BotConfig helpers (global key-value settings)
# ---------------------------------------------------------------------------

async def get_bot_config(key: str) -> Optional[str]:
    """Return the stored value for *key*, or None if not set."""
    async with get_session() as s:
        row = await s.get(BotConfig, key)
        return row.value if row and row.value else None


async def set_bot_config(key: str, value: str) -> None:
    """Upsert a key-value pair in BotConfig."""
    async with get_session() as s:
        row = await s.get(BotConfig, key)
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
        else:
            s.add(BotConfig(key=key, value=value))
        await s.commit()


async def delete_bot_config(key: str) -> None:
    """Delete a BotConfig entry if it exists."""
    async with get_session() as s:
        row = await s.get(BotConfig, key)
        if row:
            await s.delete(row)
            await s.commit()


async def get_all_bot_configs() -> dict[str, str]:
    """Return all non-null BotConfig entries as a plain dict."""
    async with get_session() as s:
        result = await s.execute(select(BotConfig))
        return {r.key: r.value for r in result.scalars().all() if r.value is not None}


# ---------------------------------------------------------------------------
# Admin RSS helper (all feeds across all chats)
# ---------------------------------------------------------------------------

async def get_all_rss_feeds() -> List[RssFeed]:
    """Return every RSS feed ordered by chat_id then id."""
    async with get_session() as s:
        result = await s.execute(
            select(RssFeed).order_by(RssFeed.chat_id, RssFeed.id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# PlatformCredential helpers
# ---------------------------------------------------------------------------

async def get_credential(platform: str) -> Optional[PlatformCredential]:
    async with get_session() as s:
        return await s.get(PlatformCredential, platform)


async def set_credential(platform: str, username: str, password_enc: str) -> None:
    async with get_session() as s:
        row = await s.get(PlatformCredential, platform)
        if row:
            row.username = username
            row.password_enc = password_enc
        else:
            s.add(PlatformCredential(platform=platform, username=username, password_enc=password_enc))
        await s.commit()


async def delete_credential(platform: str) -> None:
    async with get_session() as s:
        row = await s.get(PlatformCredential, platform)
        if row:
            await s.delete(row)
            await s.commit()


async def get_all_credentials() -> List[PlatformCredential]:
    async with get_session() as s:
        result = await s.execute(
            select(PlatformCredential).order_by(PlatformCredential.platform)
        )
        return list(result.scalars().all())


async def update_credential_refresh(platform: str, ok: bool) -> None:
    async with get_session() as s:
        row = await s.get(PlatformCredential, platform)
        if row:
            row.last_refresh_at = datetime.utcnow()
            row.last_refresh_ok = ok
            await s.commit()


# ---------------------------------------------------------------------------
# PsnLibraryGame helpers
# ---------------------------------------------------------------------------

async def get_psn_library_games(
    tier: Optional[int] = None,
    status: Optional[str] = None,
    region: str = "HK",
    keyword: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[PsnLibraryGame]:
    async with get_session() as s:
        q = select(PsnLibraryGame).where(PsnLibraryGame.region == region)
        if tier is not None:
            q = q.where(PsnLibraryGame.tier == tier)
        if status is not None:
            q = q.where(PsnLibraryGame.status == status)
        if keyword:
            kw = f"%{keyword}%"
            q = q.where(or_(
                PsnLibraryGame.title_en.ilike(kw),
                PsnLibraryGame.title_zh.ilike(kw),
            ))
        q = q.order_by(PsnLibraryGame.entry_date.desc()).limit(limit).offset(offset)
        result = await s.execute(q)
        return list(result.scalars().all())


async def count_psn_library_games(
    tier: Optional[int] = None,
    status: Optional[str] = None,
    region: str = "HK",
    keyword: Optional[str] = None,
) -> int:
    async with get_session() as s:
        q = select(func.count()).select_from(PsnLibraryGame).where(PsnLibraryGame.region == region)
        if tier is not None:
            q = q.where(PsnLibraryGame.tier == tier)
        if status is not None:
            q = q.where(PsnLibraryGame.status == status)
        if keyword:
            kw = f"%{keyword}%"
            q = q.where(or_(
                PsnLibraryGame.title_en.ilike(kw),
                PsnLibraryGame.title_zh.ilike(kw),
            ))
        return (await s.execute(q)).scalar_one()


async def get_psn_game_by_concept(concept_id: int) -> Optional[PsnLibraryGame]:
    async with get_session() as s:
        result = await s.execute(
            select(PsnLibraryGame).where(PsnLibraryGame.concept_id == concept_id)
        )
        return result.scalars().first()


async def get_psn_game_by_id(game_id: int) -> Optional[PsnLibraryGame]:
    async with get_session() as s:
        return await s.get(PsnLibraryGame, game_id)


async def search_psn_games(keyword: str) -> List[PsnLibraryGame]:
    """Search by title_en or title_zh; returns up to 5 matches ordered by tier."""
    kw = f"%{keyword}%"
    async with get_session() as s:
        result = await s.execute(
            select(PsnLibraryGame)
            .where(or_(
                PsnLibraryGame.title_en.ilike(kw),
                PsnLibraryGame.title_zh.ilike(kw),
            ))
            .order_by(PsnLibraryGame.tier, PsnLibraryGame.entry_date.desc())
            .limit(5)
        )
        return list(result.scalars().all())


async def upsert_psn_game(
    title_en: str,
    entry_date,
    tier: int,
    *,
    game_id: Optional[int] = None,
    title_zh: Optional[str] = None,
    concept_id: Optional[int] = None,
    product_id: Optional[str] = None,
    store_url: Optional[str] = None,
    cover_url: Optional[str] = None,
    release_date=None,
    exit_date=None,
    status: str = "active",
    platforms: Optional[str] = None,
    genre: Optional[str] = None,
    age_rating: Optional[str] = None,
    streaming: bool = False,
    region: str = "HK",
    note: Optional[str] = None,
) -> PsnLibraryGame:
    async with get_session() as s:
        row: Optional[PsnLibraryGame] = None
        if game_id:
            row = await s.get(PsnLibraryGame, game_id)
        if not row and product_id:
            res = await s.execute(
                select(PsnLibraryGame).where(PsnLibraryGame.product_id == product_id).limit(1)
            )
            row = res.scalars().first()
        if not row and title_en:
            res = await s.execute(
                select(PsnLibraryGame)
                .where(
                    func.lower(PsnLibraryGame.title_en) == title_en.lower(),
                    PsnLibraryGame.tier == tier,
                )
                .limit(1)
            )
            row = res.scalars().first()
        if not row:
            row = PsnLibraryGame()
            s.add(row)
        row.title_en = title_en
        row.title_zh = title_zh
        row.concept_id = concept_id
        row.product_id = product_id
        row.store_url = store_url
        row.cover_url = cover_url
        row.release_date = release_date
        row.entry_date = entry_date
        row.exit_date = exit_date
        row.tier = tier
        row.status = status
        row.platforms = platforms
        row.genre = genre
        row.age_rating = age_rating
        row.streaming = streaming
        row.region = region
        row.note = note
        await s.commit()
        await s.refresh(row)
        return row


async def match_psn_game(en_name: str, raw_query: str = "") -> Optional[PsnLibraryGame]:
    """Match a game by English name (exact → fuzzy → nospace) then by raw_query on title_zh."""
    from sqlalchemy import text as _text

    async with get_session() as s:
        # 1. Exact match
        result = await s.execute(
            select(PsnLibraryGame)
            .where(func.lower(PsnLibraryGame.title_en) == en_name.lower())
            .order_by(PsnLibraryGame.tier, PsnLibraryGame.entry_date.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row:
            return row

        # 2. LIKE %en_name%
        kw = f"%{en_name}%"
        result = await s.execute(
            select(PsnLibraryGame)
            .where(PsnLibraryGame.title_en.ilike(kw))
            .order_by(PsnLibraryGame.tier, PsnLibraryGame.entry_date.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row:
            return row

        # 3. No-space variant (handles "WuchangFallenFeathers" vs "Wuchang: Fallen Feathers")
        no_space = en_name.replace(" ", "").replace(":", "").lower()
        if len(no_space) >= 4:
            result = await s.execute(
                select(PsnLibraryGame)
                .where(_text(
                    "LOWER(REPLACE(REPLACE(title_en,' ',''),':','')) LIKE :nsp"
                ).bindparams(nsp=f"%{no_space}%"))
                .order_by(PsnLibraryGame.tier, PsnLibraryGame.entry_date.desc())
                .limit(1)
            )
            row = result.scalars().first()
            if row:
                return row

        # 4. Raw query (CJK input) matched against title_zh
        rq = (raw_query or "").strip()
        if rq and rq != en_name:
            rq_kw = f"%{rq}%"
            result = await s.execute(
                select(PsnLibraryGame)
                .where(PsnLibraryGame.title_zh.ilike(rq_kw))
                .order_by(PsnLibraryGame.tier, PsnLibraryGame.entry_date.desc())
                .limit(1)
            )
            row = result.scalars().first()
            if row:
                return row

        return None


async def delete_psn_game(game_id: int) -> bool:
    async with get_session() as s:
        row = await s.get(PsnLibraryGame, game_id)
        if not row:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def patch_psn_game_cell(game_id: int, field: str, value: str) -> bool:
    """Update a single field on a game row. Extra (non-core) fields go into extra_data JSON."""
    import json as _json
    core_fields = {c.key for c in PsnLibraryGame.__table__.columns} - {"id", "created_at"}
    async with get_session() as s:
        row = await s.get(PsnLibraryGame, game_id)
        if not row:
            return False
        if field in core_fields:
            # Type coercions for core columns
            if field in ("tier",):
                setattr(row, field, int(value) if value.strip().isdigit() else 2)
            elif field in ("entry_date", "exit_date", "release_date"):
                from datetime import date as _date
                try:
                    setattr(row, field, _date.fromisoformat(value) if value else None)
                except ValueError:
                    setattr(row, field, None)
            elif field == "streaming":
                setattr(row, field, value.lower() in ("1", "true", "yes"))
            else:
                setattr(row, field, value if value else None)
        else:
            # Store in extra_data JSON
            try:
                extra = _json.loads(row.extra_data or "{}")
            except Exception:
                extra = {}
            extra[field] = value
            row.extra_data = _json.dumps(extra, ensure_ascii=False)
        await s.commit()
        return True


# ---------------------------------------------------------------------------
# PsnColumnsConfig helpers
# ---------------------------------------------------------------------------

async def get_psn_columns() -> List[PsnColumnsConfig]:
    async with get_session() as s:
        result = await s.execute(
            select(PsnColumnsConfig).order_by(PsnColumnsConfig.sort_order, PsnColumnsConfig.col_key)
        )
        return list(result.scalars().all())


async def upsert_psn_column(
    col_key: str,
    display_name: str,
    col_type: str = "text",
    is_core: bool = False,
    visible: bool = True,
    sort_order: int = 100,
) -> PsnColumnsConfig:
    async with get_session() as s:
        row = await s.get(PsnColumnsConfig, col_key)
        if row:
            row.display_name = display_name
            row.col_type = col_type
            row.visible = visible
            row.sort_order = sort_order
        else:
            row = PsnColumnsConfig(
                col_key=col_key, display_name=display_name,
                col_type=col_type, is_core=is_core,
                visible=visible, sort_order=sort_order,
            )
            s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def delete_psn_column(col_key: str) -> bool:
    """Delete a non-core custom column."""
    async with get_session() as s:
        row = await s.get(PsnColumnsConfig, col_key)
        if not row or row.is_core:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def bulk_import_psn_games(rows: List[dict]) -> tuple[int, int]:
    """Import games from parsed CSV rows. Returns (success_count, error_count)."""
    from datetime import date as _date
    ok = 0
    err = 0
    for row in rows:
        try:
            def _d(k: str):
                v = (row.get(k) or "").strip()
                return _date.fromisoformat(v) if v else None

            concept_id = int(row["concept_id"]) if (row.get("concept_id") or "").strip() else None
            tier = int((row.get("tier") or "2").strip())
            entry_date = _d("entry_date") or _date.today()

            platforms_raw = (row.get("platforms") or "").strip()
            platforms = platforms_raw if platforms_raw else None

            await upsert_psn_game(
                title_en=(row.get("title_en") or "").strip(),
                entry_date=entry_date,
                tier=tier,
                title_zh=(row.get("title_zh") or "").strip() or None,
                concept_id=concept_id,
                product_id=(row.get("product_id") or "").strip() or None,
                store_url=(row.get("store_url") or "").strip() or None,
                cover_url=(row.get("cover_url") or "").strip() or None,
                release_date=_d("release_date"),
                exit_date=_d("exit_date"),
                status=(row.get("status") or "active").strip() or "active",
                platforms=platforms,
                genre=(row.get("genre") or "").strip() or None,
                age_rating=(row.get("age_rating") or "").strip() or None,
                streaming=(row.get("streaming") or "").strip().lower() in ("1", "true", "yes"),
                region=(row.get("region") or "HK").strip() or "HK",
                note=(row.get("note") or "").strip() or None,
            )
            ok += 1
        except Exception:
            err += 1
    return ok, err
