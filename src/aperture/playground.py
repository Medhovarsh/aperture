"""Hosted demo playground.

A thin HTTP wrapper over :class:`~aperture.plane.ContextPlane` plus a single-page UI,
so the product thesis is visible in twenty seconds without cloning anything: pick an
identity, pick a purpose, ask a question, watch records appear and disappear with
reason codes attached.

This is a **demo surface, not the product**. It deliberately does the one thing the
real server refuses to do - it lets the caller choose which principal to act as -
because that is the whole point of a playground. The real MCP server pins identity
server-side precisely so an agent cannot do this.

Everything it serves is synthetic. The workspace is generated into a temp directory
on first request, and on a serverless host the lineage log lives only for the life of
that instance.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .demo import build_demo_workspace
from .plane import ContextPlane
from .types import SearchRequest
from .workspace import Workspace

MAX_QUESTION_LENGTH = 400

_plane: ContextPlane | None = None


def get_plane() -> ContextPlane:
    """Return the shared plane, generating the demo workspace on first use."""
    global _plane
    if _plane is None:
        root = Path(tempfile.gettempdir()) / "aperture-playground"
        build_demo_workspace(root)
        _plane = ContextPlane(Workspace.load(root))
    return _plane


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
                "trace_id": entry.get("trace_id"),
                "principal_id": entry.get("principal_id"),
                "purpose": entry.get("purpose"),
                "question": entry.get("question"),
                "returned": len(entry.get("returned", [])),
                "withheld": entry.get("withheld", []),
                "hash": str(entry.get("hash", ""))[:12],
            }
            for entry in log.tail(min(limit, 25))
        ],
    }


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
    --chip: #f0f0ec; --code: #f4f4f1;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17171a; --panel: #1f1f23; --ink: #ececec; --muted: #9a9a9a;
      --line: #2e2e34; --accent: #7fa0ff; --allow: #4ecb8f; --deny: #ff8272;
      --chip: #2a2a30; --code: #232329;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 32px 20px 72px; }
  header h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: -0.02em; }
  header p { margin: 0 0 4px; color: var(--muted); }
  header a { color: var(--accent); }
  .note {
    margin: 18px 0 24px; padding: 10px 14px; border-left: 3px solid var(--accent);
    background: var(--panel); color: var(--muted); font-size: 13.5px; border-radius: 0 6px 6px 0;
  }
  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 18px; margin-bottom: 18px;
  }
  .row { display: flex; gap: 14px; flex-wrap: wrap; }
  .field { flex: 1 1 220px; min-width: 200px; }
  label { display: block; font-size: 12px; text-transform: uppercase;
          letter-spacing: 0.06em; color: var(--muted); margin-bottom: 6px; }
  select, input[type=text] {
    width: 100%; padding: 9px 10px; border: 1px solid var(--line);
    border-radius: 7px; background: var(--bg); color: var(--ink); font: inherit;
  }
  button {
    margin-top: 14px; padding: 10px 18px; border: 0; border-radius: 7px;
    background: var(--accent); color: #fff; font: inherit; font-weight: 600; cursor: pointer;
  }
  button:disabled { opacity: 0.55; cursor: default; }
  .examples { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
  .examples button {
    margin: 0; padding: 5px 11px; font-size: 12.5px; font-weight: 500;
    background: var(--chip); color: var(--ink); border: 1px solid var(--line);
  }
  .who { margin-top: 10px; font-size: 13px; color: var(--muted); }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.07em;
       color: var(--muted); margin: 0 0 12px; }
  .record { border: 1px solid var(--line); border-left: 3px solid var(--allow);
            border-radius: 7px; padding: 12px 14px; margin-bottom: 10px; }
  .record h3 { margin: 0 0 4px; font-size: 15px; }
  .meta { font-size: 12.5px; color: var(--muted); margin-bottom: 8px; }
  .body { font-size: 14px; white-space: pre-wrap; }
  .withheld { border: 1px solid var(--line); border-left: 3px solid var(--deny);
              border-radius: 7px; padding: 10px 14px; margin-bottom: 8px; }
  .withheld .code { color: var(--deny); font-weight: 600;
                    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
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
  footer { margin-top: 28px; font-size: 13px; color: var(--muted); }
  footer a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Aperture</h1>
    <p>A governed context plane for AI agents. Denial is explainable, never silent.</p>
    <p><a href="https://github.com/Medhovarsh/aperture">github.com/Medhovarsh/aperture</a></p>
  </header>

  <div class="note">
    <strong>Try this:</strong> ask <em>&ldquo;how much parental leave do we offer&rdquo;</em> as
    Dana under <code>hr_support</code>, then ask the same question as Kim under
    <code>customer_support</code>. Same corpus, same question, different answer &mdash; and the
    support agent is told what it could not see instead of quietly answering from a thinner context.
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
      <div class="field" style="flex: 2 1 340px;">
        <label for="question">Question</label>
        <input type="text" id="question" value="how much parental leave do we offer">
      </div>
    </div>
    <div class="examples" id="examples"></div>
    <button id="ask">Ask the plane</button>
  </div>

  <div id="results"></div>

  <div class="panel">
    <h2>Audit trail</h2>
    <div id="lineage">&mdash;</div>
  </div>

  <footer>
    All data here is synthetic. This playground lets you choose an identity, which the real
    MCP server refuses to do &mdash; identity is pinned server-side so a prompt injection cannot
    change it. On a serverless host the audit log lives only for the life of the instance.
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

async function load() {
  identities = await (await fetch("/api/identities")).json();

  const principal = document.getElementById("principal");
  principal.innerHTML = identities.principals
    .map(p => `<option value="${p.id}">${p.display_name}</option>`).join("");

  const purpose = document.getElementById("purpose");
  purpose.innerHTML = identities.purposes
    .map(p => `<option value="${p}">${p}</option>`).join("");

  document.getElementById("examples").innerHTML = EXAMPLES
    .map(q => `<button type="button" data-q="${q}">${q}</button>`).join("");
  document.querySelectorAll("#examples button").forEach(button => {
    button.onclick = () => {
      document.getElementById("question").value = button.dataset.q;
      ask();
    };
  });

  principal.onchange = describe;
  describe();
  ask();
}

function describe() {
  const id = document.getElementById("principal").value;
  const who = identities.principals.find(p => p.id === id);
  document.getElementById("who").innerHTML =
    `tenant <span class="tag">${who.tenant}</span>` +
    `clearance <span class="tag">${who.clearance}</span>` +
    who.groups.map(g => `<span class="tag">${g}</span>`).join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function ask() {
  const button = document.getElementById("ask");
  button.disabled = true;
  button.textContent = "Asking...";

  const payload = {
    principal_id: document.getElementById("principal").value,
    purpose: document.getElementById("purpose").value,
    question: document.getElementById("question").value
  };

  try {
    const data = await (await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })).json();
    render(data);
    await renderLineage();
  } finally {
    button.disabled = false;
    button.textContent = "Ask the plane";
  }
}

function render(data) {
  const records = data.records.map(r => `
    <div class="record">
      <h3>${escapeHtml(r.title)}</h3>
      <div class="meta">
        source <strong>${escapeHtml(r.source_id)}</strong> &middot;
        ${escapeHtml(r.sensitivity)} &middot; owner ${escapeHtml(r.owner)} &middot;
        score ${r.score}${r.age_days !== null ? " &middot; " + r.age_days + "d old" : ""}
        ${r.redacted_fields.length ? ' &middot; <strong>redacted: ' +
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

  document.getElementById("results").innerHTML = `
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

async function renderLineage() {
  const data = await (await fetch("/api/lineage")).json();
  const rows = data.entries.slice().reverse().map(e => `
    <tr>
      <td class="mono">${e.seq}</td>
      <td class="mono">${escapeHtml(e.principal_id)}</td>
      <td class="mono">${escapeHtml(e.purpose)}</td>
      <td>${escapeHtml(e.question)}</td>
      <td class="mono">${e.returned}</td>
      <td class="mono">${e.withheld.map(w => w.count + "x" + w.reason).join(", ")}</td>
      <td class="mono">${escapeHtml(e.hash)}</td>
    </tr>`).join("");

  document.getElementById("lineage").innerHTML = `
    <p class="meta">Chain integrity:
      <span class="${data.chain_intact ? "ok" : ""}">${
        data.chain_intact ? "intact" : "BROKEN"}</span>
      &mdash; every query, including every denial, is one hash-chained line.</p>
    <table>
      <tr><th>#</th><th>principal</th><th>purpose</th><th>question</th>
          <th>returned</th><th>withheld</th><th>hash</th></tr>
      ${rows}
    </table>`;
}

load();
</script>
</body>
</html>
"""
