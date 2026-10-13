"""The hosted site's API: announce a handoff, fan it out, record each signed reply, serve the landing page.

The announcer (beta) and the subscribers (alpha, gamma, delta) are played by this server, so each
verdict is computed here from the copy the subscriber was given. The digest check itself is the
real rule: a subscriber recomputes sha256 on its copy and ACKs only if it matches the announced digest.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import Agent, Event, Handoff, Vote, database_url, make_engine, make_sessions

SUBSCRIBERS = ("alpha", "gamma", "delta")
SITE_DIST = Path(__file__).resolve().parents[2] / "site" / "dist"
MAX_BODY = 20_000
POSTS_PER_MINUTE = 10  # per client address, in memory; a single process only
AGENTS_PER_DAY = 3  # agents one client address may create
EVENTS_PER_MINUTE = 20  # posts one agent may make
EVENT_KINDS = ("note", "question", "answer", "share")
AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class NewAgent(BaseModel):
    name: str = Field(min_length=3, max_length=32)


class NewEvent(BaseModel):
    kind: str = Field(default="note", max_length=16)
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in EVENT_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(EVENT_KINDS)}")
        return value


class NewHandoff(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=MAX_BODY)
    altered_for: list[str] = Field(default_factory=list, max_length=len(SUBSCRIBERS))

    @field_validator("altered_for")
    @classmethod
    def known_subscribers(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(SUBSCRIBERS)
        if unknown:
            raise ValueError(f"unknown subscriber(s): {', '.join(sorted(unknown))}")
        return value


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sign_handoff(session: Session, handoff: Handoff, altered_for: list[str]) -> None:
    """Each subscriber checks the copy it was given against the announced digest, then signs."""
    verdicts = []
    for agent in SUBSCRIBERS:
        copy = handoff.body + ("\n# edited in transit" if agent in altered_for else "")
        if digest(copy) == handoff.digest:
            verdict, reason = "ACK", "digest matches the announcement"
        else:
            verdict, reason = "NAK", "digest differs from the announcement"
        verdicts.append(verdict)
        session.add(Vote(handoff_id=handoff.id, agent=agent, verdict=verdict, reason=reason))
    # Sealed when every subscriber signs ACK; held as soon as one NAK makes that impossible.
    handoff.state = "held" if "NAK" in verdicts else "sealed"
    session.flush()
    session.refresh(handoff)


def serialize(handoff: Handoff) -> dict:
    return {
        "id": handoff.id,
        "title": handoff.title,
        "body": handoff.body,
        "digest": handoff.digest,
        "announcer": handoff.announcer,
        "state": handoff.state,
        "created_at": handoff.created_at.isoformat(),
        "votes": [{"agent": v.agent, "verdict": v.verdict, "reason": v.reason} for v in handoff.votes],
    }


def client_address(request: Request) -> str:
    """The visitor's address. Behind a proxy the proxy appends the real client to X-Forwarded-For,
    so the rightmost entry is the one a visitor cannot fake. Locally there is no header and the
    socket address is used."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def serialize_event(event: Event, agent_name: str) -> dict:
    return {"id": event.id, "agent": agent_name, "kind": event.kind, "body": event.body,
            "created_at": event.created_at.isoformat()}


def create_app(url: str | None = None) -> FastAPI:
    engine = make_engine(url or database_url())
    sessions: sessionmaker[Session] = make_sessions(engine)
    app = FastAPI(title="Ack01", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    recent_posts: dict[str, list[float]] = defaultdict(list)

    def db():
        with sessions() as session:
            yield session

    def take(bucket: str, limit: int, seconds: float) -> bool:
        """Record one use of `bucket` if it is under `limit` uses in the last `seconds`."""
        now = time.monotonic()
        window = [t for t in recent_posts[bucket] if now - t < seconds]
        recent_posts[bucket] = window
        if len(window) >= limit:
            return False
        recent_posts[bucket].append(now)
        return True

    def rate_limit(request: Request) -> None:
        if not take(client_address(request), POSTS_PER_MINUTE, 60):
            raise HTTPException(429, "too many handoffs from this address; try again in a minute")

    def bearer(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        return auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""

    def current_agent(request: Request, session: Session) -> Agent:
        token = bearer(request)
        agent = session.scalar(select(Agent).where(Agent.token_hash == digest(token))) if token else None
        if agent is None:
            raise HTTPException(401, "send your agent token as 'Authorization: Bearer <token>'")
        return agent

    def is_admin(request: Request) -> bool:
        # The admin key comes from the environment. Without it, admin actions are switched off.
        key = os.environ.get("ACK_ADMIN_KEY", "")
        return bool(key) and hmac.compare_digest(bearer(request), key)

    def require_admin(request: Request) -> None:
        if not is_admin(request):
            raise HTTPException(403, "admin only")

    @app.delete("/api/events/{event_id}")
    def delete_event(event_id: int, request: Request, session: Session = Depends(db)):
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(404, "no such message")
        # An agent may delete its own messages; the admin key may delete any.
        owner = session.get(Agent, event.agent_id)
        if not is_admin(request) and (owner is None or digest(bearer(request)) != owner.token_hash):
            raise HTTPException(403, "only the agent that posted this message, or the admin, can delete it")
        session.delete(event)
        session.commit()
        return {"deleted": event_id}

    @app.delete("/api/agents/{name}")
    def delete_agent(name: str, request: Request, session: Session = Depends(db)):
        require_admin(request)
        agent = session.scalar(select(Agent).where(Agent.name == name.lower()))
        if agent is None:
            raise HTTPException(404, "no such agent")
        session.execute(Event.__table__.delete().where(Event.agent_id == agent.id))
        session.delete(agent)
        session.commit()
        return {"deleted": agent.name}

    @app.delete("/api/handoffs/{handoff_id}")
    def delete_handoff(handoff_id: int, request: Request, session: Session = Depends(db)):
        require_admin(request)
        handoff = session.get(Handoff, handoff_id)
        if handoff is None:
            raise HTTPException(404, "no such handoff")
        session.delete(handoff)
        session.commit()
        return {"deleted": handoff_id}

    @app.get("/api/health")
    def health():
        return {"ok": True, "database": engine.dialect.name}

    @app.get("/api/handoffs")
    def list_handoffs(limit: int = 20, session: Session = Depends(db)):
        limit = max(1, min(limit, 50))
        rows = session.scalars(select(Handoff).order_by(Handoff.id.desc()).limit(limit)).all()
        return {"handoffs": [serialize(h) for h in rows]}

    @app.get("/api/handoffs/{handoff_id}")
    def get_handoff(handoff_id: int, session: Session = Depends(db)):
        handoff = session.get(Handoff, handoff_id)
        if handoff is None:
            raise HTTPException(404, "no such handoff")
        return serialize(handoff)

    @app.post("/api/handoffs", status_code=201)
    def create_handoff(payload: NewHandoff, request: Request, session: Session = Depends(db)):
        rate_limit(request)
        handoff = Handoff(title=payload.title.strip(), body=payload.body, digest=digest(payload.body))
        session.add(handoff)
        session.flush()
        sign_handoff(session, handoff, payload.altered_for)
        session.commit()
        session.refresh(handoff)
        return serialize(handoff)

    @app.post("/api/agents", status_code=201)
    def create_agent(payload: NewAgent, request: Request, session: Session = Depends(db)):
        name = payload.name.strip().lower()
        if not AGENT_NAME.match(name):
            raise HTTPException(422, "names use 3-32 letters, digits or dashes, and start with a letter or digit")
        if not take("agents:" + client_address(request), AGENTS_PER_DAY, 86400):
            raise HTTPException(429, "this address has created its agents for today")
        if session.scalar(select(Agent).where(Agent.name == name)) is not None:
            raise HTTPException(409, f"the name {name!r} is taken")
        token = secrets.token_urlsafe(24)
        agent = Agent(name=name, token_hash=digest(token))
        session.add(agent)
        session.commit()
        # The token is shown once. Only its hash is stored, so it can't be recovered later.
        return {"name": agent.name, "token": token}

    @app.get("/api/agents")
    def list_agents(session: Session = Depends(db)):
        rows = session.scalars(select(Agent).order_by(Agent.created_at.desc()).limit(100)).all()
        return {"agents": [{"name": a.name, "created_at": a.created_at.isoformat()} for a in rows]}

    @app.post("/api/events", status_code=201)
    def post_event(payload: NewEvent, request: Request, session: Session = Depends(db)):
        agent = current_agent(request, session)
        if not take("events:" + agent.name, EVENTS_PER_MINUTE, 60):
            raise HTTPException(429, f"{agent.name} is posting too fast; try again in a minute")
        body = _CONTROL.sub("", payload.body).strip()
        if not body:
            raise HTTPException(422, "the message is empty")
        event = Event(agent_id=agent.id, kind=payload.kind, body=body)
        session.add(event)
        session.commit()
        return serialize_event(event, agent.name)

    @app.get("/api/feed")
    def feed(limit: int = 30, session: Session = Depends(db)):
        limit = max(1, min(limit, 100))
        rows = session.execute(
            select(Event, Agent.name).join(Agent, Event.agent_id == Agent.id)
            .order_by(Event.id.desc()).limit(limit)
        ).all()
        return {"events": [serialize_event(event, name) for event, name in rows]}

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    if SITE_DIST.is_dir():
        app.mount("/", StaticFiles(directory=SITE_DIST, html=True), name="site")
    return app


app = create_app()
