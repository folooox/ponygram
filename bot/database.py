"""
Async database layer — SQLite via SQLAlchemy 2 + aiosqlite.

Tables
------
users           — every user seen by the bot
group_settings  — per-group configuration (welcome text, verification, etc.)
blacklist       — banned user IDs (global)
antispam_rules  — per-group anti-spam config
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    select,
    update,
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


class Blacklist(Base):
    __tablename__ = "blacklist"

    user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    added_by = Column(BigInteger, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


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
