"""
Async database layer — SQLite via SQLAlchemy 2 + aiosqlite.

Tables
------
users           — every user seen by the bot
group_settings  — per-group configuration (welcome text, verification, etc.)
blacklist       — banned user IDs (global)
rss_feeds       — per-chat RSS subscriptions
rss_sent        — deduplication log of pushed entries
bot_config      — key-value store for bot-level settings (API keys, etc.)
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
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


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------

_engine = None
_session_factory: Optional[async_sessionmaker] = None


async def init_db(database_url: str) -> None:
    """Create engine, session factory, and all tables."""
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
            select(UserWarn).where(UserWarn.chat_id == chat_id, UserWarn.user_id == user_id)
        )
        rows = result.scalars().all()
        count = len(rows)
        for row in rows:
            await s.delete(row)
        await s.commit()
        return count


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
