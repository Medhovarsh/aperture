# Aperture — Governed Context Plane for Agents

**Date:** 2026-09-02
**Status:** Approved for v1 implementation

## Problem

Enterprise AI agents get raw, ungoverned access to company data. Every deployment
hand-rolls the same broken plumbing:

- The vector index has no idea who is asking, so every agent sees every chunk.
- Permission filtering, where it exists, is a silent `WHERE` clause. The agent never
  learns that results were withheld, so it answers confidently and wrong.
- Nobody records which data a given agent answer was actually built from, so no one
  can audit or reproduce it after the fact.
- Source selection is hardcoded. Adding a data source means editing agent code.

BI solved the governed-access problem years ago (dbt semantic layer, Unity Catalog).
Agents have no equivalent. Every team rebuilds it badly.

## Solution

Aperture is the single chokepoint agents read enterprise data through. It knows four
things no current stack knows at once: **meaning**, **permission**, **freshness**, and
**why it said no**.

Exposed as an MCP server, so any MCP-speaking agent runtime (Claude, Cursor, OpenAI,
LangChain) integrates with one config line and cannot reach raw sources directly.

**Scope of v1: reads only.** Writes and tool-call governance are v2 on the same chokepoint.

## Differentiator

**Denial is explainable, never silent.**

Today: agent lacks permission -> gets fewer chunks -> answers confidently and wrong.

Aperture: agent gets results plus `withheld: 3 records (sensitivity=restricted:
principal lacks clearance)` and a `trace_id`. The agent can tell the user that an
answer is partial, and an auditor can replay exactly why.

Second differentiator: **purpose binding**. The same user asking for the same document
under a different declared purpose gets a different answer. Standard in privacy law
(GDPR purpose limitation, EU AI Act), nonexistent in agent stacks.

## Architecture

```
agent (MCP client)
   |
   v
[ MCP server ]  context.search / context.fetch / context.explain / catalog.list_sources
   |
   v
[ Policy engine ] principal + declared purpose -> eligible sources        (fail closed)
   |
   v
[ Semantic router ] question vs source descriptions -> which sources to query
   |
   v
[ Brokers ] docs | sql | vector          (pluggable; unregistered source = invisible)
   |
   v
[ Enforcement pipeline ] record ACL -> sensitivity -> redaction -> freshness -> budget
   |
   v
[ Lineage log ] hash-chained append-only JSONL (tamper evident)
   |
   v
response: records + provenance + withheld_summary + trace_id
```

## Components

1. **Catalog** — source registry. Each entry: `id`, `kind` (docs/sql/vector), `owner`,
   semantic description ("what questions this answers"), `sensitivity`, `freshness_sla`,
   ACL model. Unregistered sources are invisible to agents.

2. **Policy engine** — evaluates `(principal, purpose, source, record)` to
   `allow | deny | redact` with a machine-readable reason code. Declarative YAML.
   Fail-closed: any evaluation error denies.

3. **Brokers** — pluggable adapters returning records plus metadata (`acl`, `updated_at`,
   `source_id`). v1: local document corpus, SQLite, lexical vector index.

4. **Semantic router** — ranks registered sources against the question using their
   semantic descriptions, so agents ask questions rather than naming sources.

5. **Enforcement pipeline** — per-record ACL filter, sensitivity gate, field redaction,
   freshness gate, ranking and token budget. Every drop produces a reason code.

6. **Lineage log** — hash-chained append-only JSONL. Records principal, purpose, question,
   sources consulted, records returned, records withheld and why. Verifiable.

7. **MCP server** — hand-rolled JSON-RPC 2.0 over stdio, zero external dependencies.

8. **CLI control plane** — `init-demo`, `serve`, `policy lint`, `query`, `explain`,
   `lineage verify`.

## Data flow (single call)

1. Agent calls `context.search(question, purpose)` with an identity token.
2. Resolve principal. Unknown principal -> deny.
3. Policy determines eligible sources for `(principal, purpose)`.
4. Semantic router ranks eligible sources; top-N are queried.
5. Brokers fetch candidate records.
6. Enforcement filters and redacts per record, accumulating reason codes.
7. Rank, apply token budget, attach provenance.
8. Append lineage entry, return records plus `withheld_summary` and `trace_id`.

## Failure modes

| Condition | Behavior |
|---|---|
| Policy evaluation error | Deny, reason `policy_error` (fail closed) |
| Invalid policy file | Server refuses to start — never fail open |
| Unknown principal | Deny all, reason `unknown_principal` |
| Broker timeout or error | Partial results explicitly marked `sources_failed`, never silent |
| Unregistered source requested | Not found — catalog is the only namespace |
| Record missing ACL metadata | Treated as most restrictive, reason `missing_acl` |
| Stale beyond freshness SLA | Dropped or tagged per policy, reason `stale` |

## Testing strategy

- **Policy conformance matrix** — principals x purposes x records, asserted allow/deny.
  The auditable crown jewel; a policy change that breaks isolation fails CI.
- **Red team suite** — bypass attempts: prompt-injected "ignore policy" text in
  documents, source-id enumeration, path traversal, ACL confusion, cross-tenant leakage.
- **Lineage integrity** — mutate one historical log line, chain verification must fail.
- **Unit tests** per component; **e2e** through the MCP tool surface.

## Non-goals for v1 (YAGNI)

No web dashboard (CLI `explain` covers it). No live IdP sync (adapter interface plus
static principals file). No writes or tool governance. Three connectors only.

## Stack

Python 3.10+, pydantic v2, PyYAML, stdlib-only MCP server, SQLite. Runs fully local
with no cloud credentials.
