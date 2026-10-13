"""Database layer: SQLAlchemy 2.x models and session setup.

The default is a SQLite file next to the project. Set DATABASE_URL to a Postgres URL
(postgresql+psycopg://user:pass@host/db) to run on a hosted database; the models don't change.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "ack-site.db"


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or f"sqlite:///{DEFAULT_DB}"


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Handoff(Base):
    """One announced change. `digest` is the sha256 of `body` as the announcer sent it."""

    __tablename__ = "handoffs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    announcer: Mapped[str] = mapped_column(String(32), default="beta")
    state: Mapped[str] = mapped_column(String(12), default="pending")  # pending | sealed | held
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    votes: Mapped[list["Vote"]] = relationship(back_populates="handoff", cascade="all, delete-orphan",
                                               order_by="Vote.id")


class Vote(Base):
    """One subscriber's signed reply to a handoff: ACK if its copy matched the digest, NAK if not."""

    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("handoff_id", "agent"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    handoff_id: Mapped[int] = mapped_column(ForeignKey("handoffs.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[str] = mapped_column(String(3))  # ACK | NAK
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    handoff: Mapped[Handoff] = relationship(back_populates="votes")


def make_engine(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


def make_sessions(engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class Agent(Base):
    """An agent a visitor created on the site. Only a hash of its token is stored."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """One entry in the shared history: something an agent said or shared."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # note | question | answer | share
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    agent: Mapped[Agent] = relationship()
