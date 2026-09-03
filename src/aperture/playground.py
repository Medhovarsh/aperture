"""Hosted demo playground.

A thin HTTP wrapper over the plane and the action gateway, plus a single-page UI, so
the whole thesis is visible in a browser without cloning anything.

This is a **demo surface, not the product**, and it deliberately does two things the
real deployment refuses to do: it lets the caller pick an identity, and it exposes
approval over HTTP. That is the point of a demo, and it is exactly why the MCP server
does neither.

What it does *not* relax is isolation. Actions mutate real state, so every visitor
gets their own workspace keyed to a session cookie. Sharing one workspace would mean
one visitor's refund showing up in another's tables, and one visitor's rate limit
being consumed by everyone else.

Production concerns handled here:

* **Session isolation** with a bounded pool and least-recently-used eviction, so a
  crawler cannot fill the disk one session at a time.
* **Rate limiting** per session, tighter for actions than for reads, because actions
  write.
* **Security headers**, including a content-security policy that matches what the
  page actually needs: its own inline style and script, and nothing external.
* **Liveness and readiness probes** that answer different questions - whether the
  process is up, and whether it can actually serve a governed request.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .actions.gateway import ActionGateway
from .actions.types import ActionRefusal, Proposal
from .demo import build_demo_workspace
from .observability import configure_logging, metrics
from .plane import ContextPlane
from .types import SearchRequest
from .workspace import Workspace

MAX_QUESTION_LENGTH = 400
SESSION_COOKIE = "aperture_session"

#: How many visitor workspaces to keep. Each is a few hundred kilobytes; the cap
#: turns unbounded growth into bounded churn.
MAX_SESSIONS = 64

#: Requests per window, per session. Actions are limited separately and harder
#: because they write.
READ_LIMIT, READ_WINDOW = 90, 60.0
ACTION_LIMIT, ACTION_WINDOW = 25, 60.0

_current_session: ContextVar[str] = ContextVar("aperture_session", default="")


class SessionPool:
    """Per-visitor workspaces with a bounded size and LRU eviction."""

    def __init__(self, root: Path | None = None, capacity: int = MAX_SESSIONS) -> None:
        self.root = Path(root or Path(tempfile.gettempdir()) / "aperture-playground")
        self.capacity = capacity
        self._planes: OrderedDict[str, ContextPlane] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str) -> ContextPlane:
        """Return this session's plane, building its workspace on first use."""
        with self._lock:
            plane = self._planes.get(session_id)
            if plane is not None:
                self._planes.move_to_end(session_id)
                return plane

            workspace_root = self.root / session_id
            build_demo_workspace(workspace_root)
            plane = ContextPlane(Workspace.load(workspace_root))
            self._planes[session_id] = plane

            while len(self._planes) > self.capacity:
                evicted_id, _ = self._planes.popitem(last=False)
                shutil.rmtree(self.root / evicted_id, ignore_errors=True)
            return plane

    def reset(self) -> None:
        """Drop every session. Used by tests."""
        with self._lock:
            self._planes.clear()
            shutil.rmtree(self.root, ignore_errors=True)

    @property
    def size(self) -> int:
        """Number of live sessions."""
        return len(self._planes)


class RateLimiter:
    """Sliding-window request counter, keyed by session.

    The clock is injectable so behaviour at a window boundary can be tested
    deterministically. Asserting an exact request count against a real clock is a
    flaky test by construction: a slow run spans the window and the limiter
    correctly lets more requests through.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def allow(self, key: str, bucket: str, limit: int, window: float) -> bool:
        """Record a hit and report whether it is within the limit."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault((key, bucket), deque())
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


sessions = SessionPool()
limiter = RateLimiter()


def get_plane() -> ContextPlane:
    """The current session's plane."""
    return sessions.get(_current_session.get() or "anonymous")


def get_gateway() -> ActionGateway:
    """The current session's action gateway."""
    return get_plane().workspace.gateway()


def _render_proposal(result: Proposal | ActionRefusal) -> dict[str, Any]:
    """Shape a proposal or refusal for the UI."""
    if isinstance(result, ActionRefusal):
        return {
            "refused": True,
            "reason": str(result.reason),
            "explanation": result.explanation,
            "detail": result.detail,
        }
    return {
        "refused": False,
        "proposal_id": result.id,
        "state": str(result.state),
        "action_id": result.action_id,
        "arguments": result.arguments,
        "matched_rules": list(result.matched_rules),
        "requires_approval": result.requires_approval,
        "blast": {
            "headline": result.blast.headline(),
            "summary": result.blast.summary,
            "affected": result.blast.affected,
            "amount": result.blast.amount,
            "reversible": result.blast.reversible,
            "external_recipients": list(result.blast.external_recipients),
        },
        "approval": (
            {
                "approved": result.approval.approved,
                "decided_by": result.approval.decided_by,
                "note": result.approval.note,
            }
            if result.approval
            else None
        ),
        "execution_id": result.execution_id,
    }


class PlaygroundQuery(BaseModel):
    """A question asked through the playground UI."""

    principal_id: str
    purpose: str
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)


app = FastAPI(
    title="Aperture Playground",
    description="Interactive demo of governed, purpose-bound retrieval for AI agents.",
    docs_url="/api/docs",
)


# The page uses its own inline <style> and <script> and loads nothing external, so
# the policy can be this tight. 'unsafe-inline' is required for inline blocks; there
# is no user-supplied markup on the page, and every dynamic value is escaped.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@app.middleware("http")
async def session_and_limits(request: Request, call_next):
    """Bind a session, enforce rate limits, and set security headers.

    Reads and actions get separate budgets. A visitor exploring the search box
    should not be able to exhaust the allowance for the half of the demo that
    writes to a database.
    """
    session_id = request.cookies.get(SESSION_COOKIE) or uuid.uuid4().hex
    token = _current_session.set(session_id)

    path = request.url.path
    is_action = path.startswith("/api/actions")
    limit, window, bucket = (
        (ACTION_LIMIT, ACTION_WINDOW, "action") if is_action else (READ_LIMIT, READ_WINDOW, "read")
    )

    try:
        if path.startswith("/api/") and not limiter.allow(session_id, bucket, limit, window):
            response: Response = JSONResponse(
                {
                    "refused": True,
                    "reason": "rate_limit_exceeded",
                    "explanation": (
                        f"more than {limit} {bucket} requests in {int(window)} seconds "
                        "from this session"
                    ),
                },
                status_code=429,
            )
        else:
            response = await call_next(request)
    finally:
        _current_session.reset(token)

    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


configure_logging()


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    """Prometheus exposition endpoint.

    Deliberately label-free on anything a visitor controls. An attacker who can
    invent label values can grow a metrics store's cardinality until it falls over,
    so labels come only from purposes, action ids, and reason codes - all values the
    deployment defines.
    """
    return metrics.render()


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    """Liveness: the process is running and can serve a request."""
    return "ok"


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness: a governed request can actually be served end to end.

    Deliberately stronger than liveness. A process that is up but whose workspace
    will not load should be taken out of rotation, not sent traffic.
    """
    try:
        plane = get_plane()
        sources = len(plane.workspace.catalog)
        actions = len(plane.workspace.actions)
        chain_ok, _ = plane.workspace.lineage.verify()
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        return JSONResponse({"ready": False, "error": str(exc)}, status_code=503)

    ready = sources > 0 and chain_ok
    return JSONResponse(
        {
            "ready": ready,
            "version": __version__,
            "sources": sources,
            "actions": actions,
            "lineage_chain_intact": chain_ok,
            "live_sessions": sessions.size,
        },
        status_code=200 if ready else 503,
    )


@app.get("/api/identities")
def identities() -> dict[str, Any]:
    """Principals and purposes the UI offers, straight from the workspace."""
    plane = get_plane()
    return {
        "principals": [
            {
                "id": principal.id,
                "display_name": principal.display_name or principal.id,
                "tenant": principal.tenant,
                "groups": list(principal.groups),
                "clearance": str(principal.clearance),
            }
            for principal in plane.workspace.principals
        ],
        "purposes": list(plane.workspace.policy.purposes),
        "sources": [
            {
                "id": source.id,
                "title": source.title,
                "sensitivity": str(source.sensitivity),
                "owner": source.owner,
            }
            for source in plane.workspace.catalog
        ],
    }


@app.post("/api/search")
def search(query: PlaygroundQuery) -> JSONResponse:
    """Run a governed retrieval and return the full response, gaps included."""
    plane = get_plane()
    response = plane.search(
        query.principal_id,
        SearchRequest(question=query.question, purpose=query.purpose, max_records=5),
    )
    return JSONResponse(
        {
            "trace_id": response.trace_id,
            "summary": response.summary_line(),
            "sources_consulted": response.sources_consulted,
            "visible_sources": [
                source["id"] for source in plane.list_sources(query.principal_id, query.purpose)
            ],
            "records": [
                {
                    "id": record.id,
                    "title": record.title,
                    "text": record.text,
                    "score": record.score,
                    "source_id": record.source_id,
                    "owner": record.citation.owner,
                    "sensitivity": str(record.citation.sensitivity),
                    "age_days": record.citation.age_days,
                    "redacted_fields": list(record.redacted_fields),
                    "notes": list(record.notes),
                }
                for record in response.records
            ],
            "withheld": [
                {
                    "reason": str(group.reason),
                    "explanation": group.explanation,
                    "count": group.count,
                    "sources": list(group.sources),
                }
                for group in response.withheld
            ],
        }
    )


@app.get("/api/lineage")
def lineage(limit: int = 8) -> dict[str, Any]:
    """Recent audit entries, plus the chain integrity check."""
    plane = get_plane()
    log = plane.workspace.lineage
    ok, problems = log.verify()
    return {
        "chain_intact": ok,
        "problems": problems,
        "entries": [
            {
                "seq": entry.get("seq"),
                "kind": entry.get("kind", "search"),
                "trace_id": entry.get("trace_id"),
                "principal_id": entry.get("principal_id"),
                "purpose": entry.get("purpose"),
                # Reads log a question; actions log what was attempted and to what.
                "detail": (
                    entry.get("question")
                    or " ".join(
                        part
                        for part in (
                            entry.get("action_id"),
                            entry.get("reason"),
                            entry.get("detail"),
                        )
                        if part
                    )
                ),
                "returned": len(entry.get("returned", [])),
                "withheld": entry.get("withheld", []),
                "hash": str(entry.get("hash", ""))[:12],
            }
            for entry in log.tail(min(limit, 25))
        ],
    }


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #


class ProposeRequest(BaseModel):
    """A proposed action from the playground UI."""

    principal_id: str
    purpose: str
    action_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class DecideRequest(BaseModel):
    """A human decision made through the playground."""

    proposal_id: str
    approver_id: str
    approved: bool = True
    note: str = ""


class ExecuteRequest(BaseModel):
    """Execution or rollback of a cleared proposal."""

    proposal_id: str = ""
    execution_id: str = ""
    principal_id: str


@app.get("/api/actions")
def list_actions(principal_id: str, purpose: str) -> dict[str, Any]:
    """Actions this principal may take under this purpose."""
    return {"actions": get_gateway().list_actions(principal_id, purpose)}


@app.post("/api/actions/propose")
def propose_action(request: ProposeRequest) -> JSONResponse:
    """Price an action and return a proposal or a refusal with its reason."""
    gateway = get_gateway()
    result = gateway.propose(
        request.principal_id, request.purpose, request.action_id, request.arguments
    )
    return JSONResponse(_render_proposal(result))


@app.post("/api/actions/decide")
def decide_action(request: DecideRequest) -> JSONResponse:
    """Approve or reject a pending proposal.

    Exposed here because the playground is a demo of the whole loop. In a real
    deployment approval lives outside anything an agent can reach - the MCP server
    has no approve tool at all.
    """
    result = get_gateway().decide(
        request.proposal_id, request.approver_id, request.approved, request.note
    )
    return JSONResponse(_render_proposal(result))


@app.post("/api/actions/execute")
def execute_action(request: ExecuteRequest) -> JSONResponse:
    """Run a cleared proposal."""
    result = get_gateway().execute(request.proposal_id, request.principal_id)
    if isinstance(result, ActionRefusal):
        return JSONResponse(_render_proposal(result))
    return JSONResponse(
        {
            "refused": False,
            "executed": True,
            "execution_id": result.id,
            "action_id": result.action_id,
            "result": result.result,
            "reversible": result.reversible,
        }
    )


@app.post("/api/actions/rollback")
def rollback_action(request: ExecuteRequest) -> JSONResponse:
    """Undo an execution that recorded a compensating operation."""
    result = get_gateway().rollback(request.execution_id, request.principal_id)
    if isinstance(result, ActionRefusal):
        return JSONResponse(_render_proposal(result))
    return JSONResponse(
        {
            "refused": False,
            "rolled_back": True,
            "execution_id": result.id,
            "result": result.rollback_result,
        }
    )


@app.get("/api/state")
def operations_state() -> dict[str, Any]:
    """Live contents of the systems actions operate on, so effects are visible."""
    import sqlite3

    plane = get_plane()
    path = plane.workspace.root / "data" / "ops.db"
    if not path.is_file():
        return {"customers": [], "tickets": [], "refunds": [], "messages": []}
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("customers", "tickets", "refunds", "messages")
        }
    finally:
        connection.close()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the single-page UI."""
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aperture - governed context plane for AI agents</title>
<style>
  :root {
    --bg: #f7f7f5; --panel: #ffffff; --ink: #1a1a1a; --muted: #6b6b6b;
    --line: #e3e3df; --accent: #2f5bd7; --allow: #0f7a4a; --deny: #b23a2f;
    --warn: #a06000; --chip: #f0f0ec; --code: #f4f4f1;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17171a; --panel: #1f1f23; --ink: #ececec; --muted: #9a9a9a;
      --line: #2e2e34; --accent: #7fa0ff; --allow: #4ecb8f; --deny: #ff8272;
      --warn: #e0a94a; --chip: #2a2a30; --code: #232329;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 32px 20px 72px; }
  .hero { padding: 22px 0 26px; border-bottom: 1px solid var(--line); margin-bottom: 22px; }
  .eyebrow { margin: 0 0 12px; font-size: 12px; text-transform: uppercase;
             letter-spacing: 0.08em; color: var(--muted); }
  .hero h1 { margin: 0 0 14px; font-size: 34px; line-height: 1.2;
             letter-spacing: -0.03em; font-weight: 700; }
  .lede { margin: 0 0 18px; font-size: 16px; color: var(--muted); max-width: 62ch; }
  .cta { display: flex; gap: 10px; flex-wrap: wrap; }
  .button {
    display: inline-block; padding: 9px 16px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel); color: var(--ink);
    text-decoration: none; font-size: 14px; font-weight: 600;
  }
  .button.primary-link { background: var(--accent); border-color: var(--accent); color: #fff; }
  .pillars { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
             gap: 14px; margin-bottom: 24px; }
  .pillar { background: var(--panel); border: 1px solid var(--line);
            border-radius: 10px; padding: 16px 18px; }
  .pillar h3 { margin: 0 0 8px; font-size: 15px; }
  .pillar p { margin: 0; font-size: 13.5px; color: var(--muted); }
  .pillar code { background: var(--chip); padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
  .anchor { scroll-margin-top: 20px; }
  @media (max-width: 600px) { .hero h1 { font-size: 26px; } }
  .note {
    margin: 18px 0 20px; padding: 10px 14px; border-left: 3px solid var(--accent);
    background: var(--panel); color: var(--muted); font-size: 13.5px; border-radius: 0 6px 6px 0;
  }
  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 18px; margin-bottom: 18px;
  }
  .tabs { display: flex; gap: 6px; margin-bottom: 18px; }
  .tabs button {
    margin: 0; padding: 9px 16px; border: 1px solid var(--line); border-radius: 8px;
    background: var(--panel); color: var(--muted); font: inherit; font-weight: 600;
    cursor: pointer;
  }
  .tabs button[aria-selected="true"] { background: var(--accent); color: #fff; border-color: var(--accent); }
  .row { display: flex; gap: 14px; flex-wrap: wrap; }
  .field { flex: 1 1 220px; min-width: 190px; }
  label { display: block; font-size: 12px; text-transform: uppercase;
          letter-spacing: 0.06em; color: var(--muted); margin-bottom: 6px; }
  select, input[type=text] {
    width: 100%; padding: 9px 10px; border: 1px solid var(--line);
    border-radius: 7px; background: var(--bg); color: var(--ink); font: inherit;
  }
  button.primary {
    margin-top: 14px; padding: 10px 18px; border: 0; border-radius: 7px;
    background: var(--accent); color: #fff; font: inherit; font-weight: 600; cursor: pointer;
  }
  button.primary:disabled { opacity: 0.55; cursor: default; }
  button.secondary {
    margin-top: 10px; margin-right: 8px; padding: 8px 14px; border: 1px solid var(--line);
    border-radius: 7px; background: var(--chip); color: var(--ink); font: inherit;
    font-weight: 600; cursor: pointer;
  }
  button.danger { border-color: var(--deny); color: var(--deny); }
  .examples { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
  .examples button {
    margin: 0; padding: 5px 11px; font-size: 12.5px; font-weight: 500;
    background: var(--chip); color: var(--ink); border: 1px solid var(--line);
    border-radius: 7px; cursor: pointer;
  }
  .who { margin-top: 10px; font-size: 13px; color: var(--muted); }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.07em;
       color: var(--muted); margin: 0 0 12px; }
  .record { border: 1px solid var(--line); border-left: 3px solid var(--allow);
            border-radius: 7px; padding: 12px 14px; margin-bottom: 10px; }
  .record h3 { margin: 0 0 4px; font-size: 15px; }
  .meta { font-size: 12.5px; color: var(--muted); margin-bottom: 8px; }
  .body { font-size: 14px; white-space: pre-wrap; }
  .withheld, .refused { border: 1px solid var(--line); border-left: 3px solid var(--deny);
              border-radius: 7px; padding: 10px 14px; margin-bottom: 8px; }
  .code { color: var(--deny); font-weight: 600;
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  .blast { border: 1px solid var(--line); border-left: 3px solid var(--warn);
           border-radius: 7px; padding: 12px 14px; margin-bottom: 10px; }
  .blast.irreversible { border-left-color: var(--deny); }
  .blast .headline { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                     font-size: 13.5px; }
  .tag { display: inline-block; background: var(--chip); border: 1px solid var(--line);
         border-radius: 20px; padding: 1px 9px; font-size: 12px; margin-right: 5px; }
  .summary { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: 13px; background: var(--code); border: 1px solid var(--line);
             border-radius: 7px; padding: 10px 12px; margin-bottom: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
  .ok { color: var(--allow); font-weight: 600; }
  .scroll { overflow-x: auto; }
  footer { margin-top: 28px; font-size: 13px; color: var(--muted); }
  footer a { color: var(--accent); }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="eyebrow">Open source &middot; Apache-2.0 &middot; no paid dependencies</p>
    <h1>Agents can't be trusted with your data<br>until you can prove what they saw.</h1>
    <p class="lede">
      Aperture is the chokepoint enterprise agents read and act through. It knows what
      the data means, who may see it, how fresh it is &mdash; and how to explain a refusal
      instead of silently returning less.
    </p>
    <div class="cta">
      <a class="button primary-link" href="#try">Try it below</a>
      <a class="button" href="https://github.com/Medhovarsh/aperture">GitHub</a>
      <a class="button" href="https://github.com/Medhovarsh/aperture/blob/main/docs/THREAT_MODEL.md">Threat model</a>
      <a class="button" href="https://github.com/Medhovarsh/aperture/blob/main/docs/COMPLIANCE.md">Control mapping</a>
    </div>
  </header>

  <section class="pillars">
    <div class="pillar">
      <h3>Denial is explainable</h3>
      <p>
        Today an agent without permission just gets fewer chunks and answers
        confidently anyway. Aperture returns the reason codes and tells the model to
        disclose them.
      </p>
    </div>
    <div class="pillar">
      <h3>Actions are priced first</h3>
      <p>
        <code>region=legacy</code> is one short string. It is also 7 deleted accounts
        and $10,320. The blast radius is measured against real state before anything
        runs &mdash; never asserted by the agent.
      </p>
    </div>
    <div class="pillar">
      <h3>The audit survives an auditor</h3>
      <p>
        Every read and every refusal is one hash-chained line. Signed checkpoints
        detect a wholesale rewrite, which a plain log cannot.
      </p>
    </div>
  </section>

  <div class="anchor" id="try"></div>

  <div class="tabs" role="tablist">
    <button id="tab-read" role="tab" aria-selected="true">Reads</button>
    <button id="tab-act" role="tab" aria-selected="false">Actions</button>
  </div>

  <div class="panel">
    <div class="row">
      <div class="field">
        <label for="principal">Acting as</label>
        <select id="principal"></select>
        <div class="who" id="who"></div>
      </div>
      <div class="field">
        <label for="purpose">Declared purpose</label>
        <select id="purpose"></select>
      </div>
    </div>
  </div>

  <!-- READS ------------------------------------------------------------ -->
  <section id="view-read">
    <div class="note">
      <strong>Try this:</strong> ask <em>&ldquo;how much parental leave do we offer&rdquo;</em> as
      Dana under <code>hr_support</code>, then as Kim under <code>customer_support</code>.
      Same corpus, same question, different answer &mdash; and the support agent is told what it
      could not see instead of quietly answering from a thinner context.
    </div>
    <div class="panel">
      <div class="field" style="min-width:100%">
        <label for="question">Question</label>
        <input type="text" id="question" value="how much parental leave do we offer">
      </div>
      <div class="examples" id="examples"></div>
      <button class="primary" id="ask">Ask the plane</button>
    </div>
    <div id="results"></div>
  </section>

  <!-- ACTIONS ---------------------------------------------------------- -->
  <section id="view-act" hidden>
    <div class="note">
      <strong>Try this:</strong> as <em>Support Copilot</em> under <code>customer_support</code>,
      refund 50 USD &mdash; it runs. Refund 3000 &mdash; it waits for a human. Refund 9000 &mdash;
      it is refused. Then switch to <em>Ana Duarte</em> under <code>data_retention</code> and purge
      region <code>legacy</code>: one short argument, seven deleted accounts, refused on impact.
    </div>
    <div class="panel">
      <div class="row">
        <div class="field">
          <label for="action">Action</label>
          <select id="action"></select>
        </div>
      </div>
      <div id="action-desc" class="who"></div>
      <div class="row" id="action-args" style="margin-top:14px"></div>
      <button class="primary" id="propose">Propose action</button>
    </div>
    <div id="action-result"></div>
    <div class="panel">
      <h2>Systems these actions touch</h2>
      <div class="scroll" id="ops-state">&mdash;</div>
    </div>
  </section>

  <div class="panel">
    <h2>Audit trail</h2>
    <div class="scroll" id="lineage">&mdash;</div>
  </div>

  <footer>
    All data here is synthetic. This playground lets you choose an identity and approve
    actions, which the real deployment refuses to do &mdash; the MCP server pins identity
    server-side and exposes no approval tool, so nothing a prompt can influence reaches
    either. On a serverless host the audit log lives only for the life of the instance.
  </footer>
</div>

<script>
const EXAMPLES = [
  "how much parental leave do we offer",
  "Raj Mehta platform engineer manager location",
  "what is the refund window for annual plans",
  "how do I fail over the database",
  "termination severance schedule",
  "globex partner onboarding seats"
];

let identities = null;
let actionSpecs = [];
let lastProposal = null;
let lastExecution = null;

function $(id) { return document.getElementById(id); }

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  return response.json();
}

async function load() {
  identities = await (await fetch("/api/identities")).json();

  $("principal").innerHTML = identities.principals
    .map(p => `<option value="${p.id}">${escapeHtml(p.display_name)}</option>`).join("");
  $("purpose").innerHTML = identities.purposes
    .map(p => `<option value="${p}">${p}</option>`).join("");

  $("examples").innerHTML = EXAMPLES
    .map(q => `<button type="button" data-q="${escapeHtml(q)}">${escapeHtml(q)}</button>`).join("");
  document.querySelectorAll("#examples button").forEach(button => {
    button.onclick = () => { $("question").value = button.dataset.q; ask(); };
  });

  $("principal").onchange = () => { describe(); refreshActions(); };
  $("purpose").onchange = refreshActions;
  $("action").onchange = renderArguments;
  $("ask").onclick = ask;
  $("propose").onclick = propose;
  $("tab-read").onclick = () => showTab("read");
  $("tab-act").onclick = () => showTab("act");

  describe();
  await ask();
  await refreshActions();
}

function showTab(which) {
  $("tab-read").setAttribute("aria-selected", String(which === "read"));
  $("tab-act").setAttribute("aria-selected", String(which === "act"));
  $("view-read").hidden = which !== "read";
  $("view-act").hidden = which !== "act";
  if (which === "act") { refreshActions(); renderOpsState(); }
}

function describe() {
  const who = identities.principals.find(p => p.id === $("principal").value);
  $("who").innerHTML =
    `tenant <span class="tag">${escapeHtml(who.tenant)}</span>` +
    `clearance <span class="tag">${escapeHtml(who.clearance)}</span>` +
    who.groups.map(g => `<span class="tag">${escapeHtml(g)}</span>`).join("");
}

/* ---------------- reads ---------------- */

async function ask() {
  const button = $("ask");
  button.disabled = true;
  button.textContent = "Asking...";
  try {
    const data = await postJson("/api/search", {
      principal_id: $("principal").value,
      purpose: $("purpose").value,
      question: $("question").value
    });
    renderSearch(data);
    await renderLineage();
  } finally {
    button.disabled = false;
    button.textContent = "Ask the plane";
  }
}

function renderSearch(data) {
  const records = data.records.map(r => `
    <div class="record">
      <h3>${escapeHtml(r.title)}</h3>
      <div class="meta">
        source <strong>${escapeHtml(r.source_id)}</strong> &middot;
        ${escapeHtml(r.sensitivity)} &middot; owner ${escapeHtml(r.owner)} &middot;
        score ${r.score}${r.age_days !== null ? " &middot; " + r.age_days + "d old" : ""}
        ${r.redacted_fields.length ? " &middot; <strong>redacted: " +
          escapeHtml(r.redacted_fields.join(", ")) + "</strong>" : ""}
      </div>
      <div class="body">${escapeHtml(r.text)}</div>
      ${r.notes.map(n => `<div class="meta">note: ${escapeHtml(n)}</div>`).join("")}
    </div>`).join("") || '<p class="meta">No records returned.</p>';

  const withheld = data.withheld.map(w => `
    <div class="withheld">
      <span class="code">${escapeHtml(w.reason)}</span> &times; ${w.count}
      <div class="meta">${escapeHtml(w.explanation)}${
        w.sources.length ? " &mdash; " + escapeHtml(w.sources.join(", ")) : ""}</div>
    </div>`).join("") || '<p class="meta">Nothing withheld.</p>';

  $("results").innerHTML = `
    <div class="panel">
      <div class="summary">${escapeHtml(data.summary)}
        <br>trace ${escapeHtml(data.trace_id)}
        &middot; routed to: ${escapeHtml(data.sources_consulted.join(", ") || "(none eligible)")}
        &middot; reachable sources: ${escapeHtml(data.visible_sources.join(", ") || "(none)")}
      </div>
      <h2>Returned</h2>${records}
      <h2 style="margin-top:18px">Withheld &mdash; and why</h2>${withheld}
    </div>`;
}

/* ---------------- actions ---------------- */

async function refreshActions() {
  const params = new URLSearchParams({
    principal_id: $("principal").value,
    purpose: $("purpose").value
  });
  const data = await (await fetch("/api/actions?" + params)).json();
  actionSpecs = data.actions;

  if (!actionSpecs.length) {
    $("action").innerHTML = "<option value=''>(no actions available)</option>";
    $("action-desc").textContent =
      "This identity may take no actions under this purpose.";
    $("action-args").innerHTML = "";
    $("propose").disabled = true;
    return;
  }
  $("propose").disabled = false;
  $("action").innerHTML = actionSpecs
    .map(a => `<option value="${a.id}">${escapeHtml(a.id)}</option>`).join("");
  renderArguments();
}

function currentSpec() {
  return actionSpecs.find(a => a.id === $("action").value);
}

function renderArguments() {
  const spec = currentSpec();
  if (!spec) return;
  $("action-desc").innerHTML =
    escapeHtml(spec.description) +
    `<br><span class="tag">${spec.effect_class}</span>` +
    `<span class="tag">${spec.reversible ? "reversible" : "IRREVERSIBLE"}</span>` +
    (spec.requires_approval ? '<span class="tag">needs approval</span>' : "");

  const defaults = {
    customer_id: "cus-4471", amount: "3000", ticket_id: "tkt-1180",
    region: "legacy", to: "ops@rivera.example", subject: "About your refund",
    body: "Your refund has been issued."
  };
  $("action-args").innerHTML = Object.entries(spec.parameters).map(([name, meta]) => `
    <div class="field">
      <label for="arg-${name}">${escapeHtml(name)} (${meta.type})</label>
      <input type="text" id="arg-${name}" data-type="${meta.type}"
             value="${escapeHtml(defaults[name] || "")}">
    </div>`).join("");
}

function collectArguments() {
  const spec = currentSpec();
  const args = {};
  Object.entries(spec.parameters).forEach(([name, meta]) => {
    const raw = $("arg-" + name).value;
    if (meta.type === "number") args[name] = parseFloat(raw);
    else if (meta.type === "integer") args[name] = parseInt(raw, 10);
    else if (meta.type === "boolean") args[name] = ["1", "true", "yes"].includes(raw.toLowerCase());
    else args[name] = raw;
  });
  return args;
}

async function propose() {
  const button = $("propose");
  button.disabled = true;
  button.textContent = "Pricing...";
  try {
    const data = await postJson("/api/actions/propose", {
      principal_id: $("principal").value,
      purpose: $("purpose").value,
      action_id: $("action").value,
      arguments: collectArguments()
    });
    lastProposal = data.refused ? null : data;
    lastExecution = null;
    renderAction(data);
    await renderLineage();
    await renderOpsState();
  } finally {
    button.disabled = false;
    button.textContent = "Propose action";
  }
}

function renderAction(data, extra) {
  if (data.refused) {
    $("action-result").innerHTML = `
      <div class="panel">
        <div class="refused">
          <span class="code">${escapeHtml(data.reason)}</span>
          <div class="meta">${escapeHtml(data.explanation)}</div>
          ${data.detail ? `<div class="meta mono">${escapeHtml(data.detail)}</div>` : ""}
        </div>
        <p class="meta">Nothing happened. The refusal carries a reason code the agent
        is required to relay.</p>
      </div>`;
    return;
  }

  const blast = data.blast;
  const approvers = identities.principals
    .filter(p => p.id !== data.arguments_principal && p.id !== $("principal").value)
    .map(p => `<option value="${p.id}">${escapeHtml(p.display_name)}</option>`).join("");

  let controls = "";
  if (data.state === "pending_approval") {
    controls = `
      <h2 style="margin-top:16px">Waiting on a human</h2>
      <div class="row">
        <div class="field">
          <label for="approver">Approve as</label>
          <select id="approver">${approvers}</select>
        </div>
      </div>
      <button class="secondary" id="approve">Approve</button>
      <button class="secondary danger" id="deny">Reject</button>
      <p class="meta">The proposer cannot approve its own action, and the approver must
      belong to a group the action names.</p>`;
  } else if (data.state === "ready") {
    controls = `<button class="secondary" id="execute">Execute</button>`;
  } else if (data.state === "executed") {
    controls = `<p class="meta">Executed.</p>`;
  }

  $("action-result").innerHTML = `
    <div class="panel">
      <div class="summary">proposal ${escapeHtml(data.proposal_id)} &middot;
        state <strong>${escapeHtml(data.state)}</strong> &middot;
        rules: ${escapeHtml(data.matched_rules.join(", ") || "(none)")}</div>
      <h2>Blast radius &mdash; measured, not asserted</h2>
      <div class="blast ${blast.reversible ? "" : "irreversible"}">
        <div class="headline">${escapeHtml(blast.headline)}</div>
      </div>
      ${data.approval ? `<p class="meta">${data.approval.approved ? "approved" : "rejected"}
        by ${escapeHtml(data.approval.decided_by)}
        ${data.approval.note ? "&mdash; " + escapeHtml(data.approval.note) : ""}</p>` : ""}
      ${extra || ""}
      ${controls}
    </div>`;

  if ($("approve")) $("approve").onclick = () => decide(true);
  if ($("deny")) $("deny").onclick = () => decide(false);
  if ($("execute")) $("execute").onclick = execute;
  if ($("rollback")) $("rollback").onclick = rollback;
}

async function decide(approved) {
  const data = await postJson("/api/actions/decide", {
    proposal_id: lastProposal.proposal_id,
    approver_id: $("approver").value,
    approved: approved,
    note: approved ? "reviewed in the playground" : "rejected in the playground"
  });
  if (!data.refused) lastProposal = data;
  renderAction(data);
  await renderLineage();
}

async function execute() {
  const data = await postJson("/api/actions/execute", {
    proposal_id: lastProposal.proposal_id,
    principal_id: $("principal").value
  });
  if (data.refused) { renderAction(data); await renderLineage(); return; }
  lastExecution = data;
  renderAction(
    Object.assign({}, lastProposal, { state: "executed" }),
    `<h2 style="margin-top:16px">Result</h2>
     <div class="record"><div class="mono">${escapeHtml(JSON.stringify(data.result))}</div></div>
     ${data.reversible
        ? '<button class="secondary" id="rollback">Undo it</button>'
        : '<p class="meta">This action cannot be undone.</p>'}`
  );
  await renderLineage();
  await renderOpsState();
}

async function rollback() {
  const data = await postJson("/api/actions/rollback", {
    execution_id: lastExecution.execution_id,
    principal_id: $("approver") ? $("approver").value : $("principal").value
  });
  renderAction(
    Object.assign({}, lastProposal, { state: "executed" }),
    data.refused
      ? `<div class="refused"><span class="code">${escapeHtml(data.reason)}</span>
         <div class="meta">${escapeHtml(data.explanation)}</div></div>`
      : `<h2 style="margin-top:16px">Rolled back</h2>
         <div class="record"><div class="mono">${escapeHtml(JSON.stringify(data.result))}</div></div>`
  );
  await renderLineage();
  await renderOpsState();
}

async function renderOpsState() {
  const data = await (await fetch("/api/state")).json();
  const table = (name, rows) => {
    if (!rows.length) return `<p class="meta">${name}: empty</p>`;
    const columns = Object.keys(rows[0]);
    return `<h2 style="margin-top:14px">${name}</h2><table>
      <tr>${columns.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr>
      ${rows.map(row => `<tr>${columns.map(c =>
        `<td class="mono">${escapeHtml(row[c])}</td>`).join("")}</tr>`).join("")}
    </table>`;
  };
  $("ops-state").innerHTML =
    table("customers", data.customers) +
    table("tickets", data.tickets) +
    table("refunds", data.refunds) +
    table("messages", data.messages);
}

async function renderLineage() {
  const data = await (await fetch("/api/lineage?limit=12")).json();
  const rows = data.entries.slice().reverse().map(e => `
    <tr>
      <td class="mono">${e.seq}</td>
      <td class="mono">${escapeHtml(e.kind)}</td>
      <td class="mono">${escapeHtml(e.principal_id || "")}</td>
      <td class="mono">${escapeHtml(e.purpose || "")}</td>
      <td>${escapeHtml(e.detail || "")}</td>
      <td class="mono">${e.returned || ""}</td>
      <td class="mono">${(e.withheld || []).map(w => w.count + "x" + w.reason).join(", ")}</td>
      <td class="mono">${escapeHtml(e.hash)}</td>
    </tr>`).join("");

  $("lineage").innerHTML = `
    <p class="meta">Chain integrity:
      <span class="${data.chain_intact ? "ok" : ""}">${
        data.chain_intact ? "intact" : "BROKEN"}</span>
      &mdash; reads and actions share one hash chain, including every refusal.</p>
    <table>
      <tr><th>#</th><th>kind</th><th>principal</th><th>purpose</th><th>detail</th>
          <th>returned</th><th>withheld</th><th>hash</th></tr>
      ${rows}
    </table>`;
}

load();
</script>
</body>
</html>
"""
