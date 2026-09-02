# Aperture

[![CI](https://github.com/Medhovarsh/aperture/actions/workflows/ci.yml/badge.svg)](https://github.com/Medhovarsh/aperture/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-stdio%20server-8A2BE2.svg)](https://modelcontextprotocol.io)
[![Dependencies](https://img.shields.io/badge/paid%20dependencies-none-brightgreen.svg)](#)

**A governed context plane for AI agents.**

Agents read enterprise data through one chokepoint that knows four things at once:
what the data *means*, who is *permitted* to see it, how *fresh* it is, and how to
*explain a refusal*.

Runs as an MCP server. Zero paid dependencies, no model API key, no vector database,
no cloud account. It installs inside a locked-down network.

![Same question, two purposes, two different answers](docs/demo.svg)

*Generated from real CLI output by `python tools/render_demo.py` - the picture cannot
drift away from what the tool actually prints.*

---

## The problem

Every enterprise agent deployment rebuilds the same broken plumbing:

- The vector index has no idea who is asking, so every agent sees every chunk.
- Permission filtering, where it exists at all, is a silent `WHERE` clause. The agent
  never learns that results were withheld, so it answers confidently and wrong.
- Nobody records which data a given answer was built from, so no one can audit or
  reproduce it afterward.
- Source selection is hardcoded, so adding a data source means editing agent code.

BI solved governed access years ago — dbt's semantic layer, Unity Catalog. Agents have
no equivalent. Aperture is that layer.

## The differentiator

**Denial is explainable, never silent.**

Today, an agent that lacks permission just gets fewer chunks and answers anyway.
Aperture returns the records the caller may see *plus* a structured account of what
was withheld and why, and instructs the model to disclose it:

```
1 record(s) returned | 3 withheld (3 purpose_not_permitted)
```

**Purpose binding.** Access is a function of identity *and* declared purpose. The same
person asking the same question under a different purpose gets different data — standard
in privacy law (GDPR purpose limitation), absent from every agent stack.

---

## Quickstart

```bash
pip install -e .
```

```bash
aperture demo --path workspace
```

That builds a small multi-tenant company: HR handbook, engineering runbooks, a support
knowledge base, and an employee database — with real ACLs, classifications, and
freshness SLAs.

### The 30-second demo

Same question. Same corpus. Different declared purpose.

```bash
aperture query -w workspace -p u_dana --purpose hr_support "how much parental leave do we offer"
```

```
trace trc_5cf638c96e10   principal=u_dana   purpose=hr_support
routed to: hr_handbook

[1] Parental Leave Policy  (score 2.5263)
    source=hr_handbook  sensitivity=confidential  30.8d old
    Birthing parents receive 18 weeks of fully paid leave...

Withheld:
    2 x purpose_not_permitted - the declared purpose is not permitted for this source  [eng_runbooks, support_kb]
```

```bash
aperture query -w workspace -p u_kim --purpose customer_support "how much parental leave do we offer"
```

```
routed to: support_kb

Withheld:
    3 x purpose_not_permitted - the declared purpose is not permitted for this source  [eng_runbooks, hr_handbook, people_db]
```

The support agent does not silently get a thin answer. It gets told what it could not see.

### Field-level redaction

```bash
aperture query -w workspace -p u_dana --purpose hr_support "Raj Mehta platform engineer manager location"
```

```
[1] Raj Mehta  (score 5.7349)
    source=people_db  sensitivity=restricted  30.0d old
    redacted: national_id, salary
    Raj Mehta Staff Platform Engineer Ana Duarte Bengaluru
```

The row stays useful. Compensation and national ID are removed from the structured
fields *and* scrubbed from the free text, because a salary quoted in a note is still a
leaked salary.

### Audit

```bash
aperture lineage -w workspace tail
aperture explain -w workspace trc_5cf638c96e10
aperture lineage -w workspace verify
```

```
Lineage chain intact across 6 entries.
```

Every query — including every denial — is one hash-chained line in an append-only log:
who asked, under what purpose, which sources were consulted, what came back, what was
withheld and why. Edit any historical line and `verify` names the broken entry.

---

## Connect it to an agent

Aperture speaks MCP over stdio. In `claude_desktop_config.json` (or any MCP client):

```json
{
  "mcpServers": {
    "aperture": {
      "command": "aperture",
      "args": [
        "serve",
        "-w", "/abs/path/to/workspace",
        "-p", "svc_support_agent",
        "--purpose", "customer_support"
      ]
    }
  }
}
```

Tools exposed: `context_search`, `context_fetch`, `catalog_list_sources`, `access_explain`.

---

## Try it in a browser

A hosted playground ships in the repo: pick an identity, pick a purpose, ask a question,
and watch records appear and disappear with their reason codes, plus the live audit trail.

```bash
pip install -e ".[web]"
uvicorn aperture.playground:app --reload
```

Deploy it anywhere that runs a Python function. For Vercel:

```bash
npm i -g vercel && vercel deploy
```

`vercel.json` and `api/index.py` are already wired; the whole app is one function.

The playground deliberately does the one thing the real server refuses to do - it lets
**you** choose which principal to act as. That is the point of a demo, and it is exactly
why the MCP server pins identity server-side: anything a caller can choose, a prompt
injection can choose too. All playground data is synthetic, and on a serverless host the
audit log lives only for the life of the instance.

**The identity is on the server, not in the tool call.** One server instance acts as
exactly one principal. An agent cannot choose who it is, because anything an agent can
put in a tool argument, a prompt injection can put there too. `--allow-principal-override`
exists for local development and says so loudly.

---

## How it works

```
agent (MCP client)
   |
   v  context_search(question, purpose)
[ policy engine ]   principal + purpose -> eligible sources          fail closed
   |
   v
[ semantic router ] question vs source descriptions -> which to query
   |
   v
[ brokers ]         docs | sql | vector      unregistered source = invisible
   |
   v
[ enforcement ]     tenant -> ACL -> clearance -> policy -> freshness
                    -> redaction -> budget      every drop has a reason code
   |
   v
[ lineage ]         hash-chained append-only log
   |
   v
records + provenance + withheld_summary + trace_id
```

### Configuration

A workspace is three YAML documents and the data they point at.

**`catalog.yaml`** — the only namespace agents can reach. The `description` is
load-bearing: the router matches questions against it.

```yaml
sources:
  - id: hr_handbook
    kind: docs
    title: HR Handbook
    description: >-
      Employee handbook and people policies: parental leave, paid time off,
      expenses, remote work, performance review cycles.
    owner: people-ops@acme.example
    sensitivity: confidential
    freshness_sla_days: 400
    allowed_purposes: [hr_support, security_audit]
    config:
      path: data/hr
```

**`policy.yaml`** — default deny, deny overrides allow, redactions accumulate.

```yaml
purposes: [hr_support, engineering_oncall, customer_support, security_audit]

defaults:
  stale_action: tag        # or "drop"

rules:
  - id: employees-read-internal
    effect: allow
    when:
      groups: [employees]
      tenants: [acme]
      sensitivity_at_most: internal

  - id: hr-reads-directory
    effect: allow
    when:
      groups: [hr]
      purposes: [hr_support]
      sources: [people_db]

  - id: redact-compensation
    effect: redact
    when:
      sources: [people_db]
    redact_fields: [salary, national_id]

  - id: deny-partners-confidential
    effect: deny
    when:
      groups: [partners]
      sensitivity_at_least: confidential
```

**`principals.yaml`** — identities, groups, clearance, tenant.

`aperture lint` validates all three and refuses a policy that references a source that
does not exist. An unparseable policy stops the server; it never degrades into an open one.

### Connectors

| kind | backing store | notes |
|---|---|---|
| `docs` | Markdown directory | governance metadata in YAML frontmatter |
| `sql` | SQLite | every column reaches enforcement, so redaction is field-precise |
| `vector` | JSONL chunk store | cosine when embeddings are present, BM25 otherwise — and it says which |

Adding a connector means implementing one `search` method. Brokers do no
authorization at all; that separation is what keeps the security surface small.

---

## Security model

Enforced today:

- **Default deny.** No matching allow rule means no access.
- **Fail closed.** A crash inside policy evaluation returns deny, never an exception
  and never an allow.
- **Missing metadata is restrictive.** A record with no ACL is withheld, not shared.
- **Identity is server-side.** Prompt text and tool arguments cannot influence it.
- **Path confinement.** A catalog entry cannot read outside the workspace.
- **SQL identifiers come from the catalog** and are validated against the live schema.
- **Record ids are not capabilities.** Permissions are re-evaluated on every fetch.
- **Tenant isolation** is the first gate and is unconditional.

The red-team suite (`tests/test_redteam.py`) probes each of these, including a poisoned
document that instructs the system to override policy. It is retrieved as ordinary text
and changes nothing, because authorization is computed from the principal and the policy
and nothing an attacker can write into an index participates in that decision.

### What is *not* claimed

- **The lineage log is tamper-evident, not tamper-proof.** Someone with write access can
  rewrite the whole file consistently. Detecting that needs the head hash anchored
  somewhere they do not control. Not built.
- **Reads only.** Tool-call and write governance are v2.
- **Identity is a static file.** The registry sits behind an interface so an IdP can
  back it later; no IdP integration ships today.
- **Retrieval is lexical BM25.** Deliberate — it makes the plane installable with no
  model download. Swap in a real embedding index without touching any other module.
- **No web dashboard.** `aperture explain` and `aperture lineage` cover the operator loop.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

104 tests, run on Python 3.10-3.13 plus Windows and macOS by CI. The two that matter most:

- **`tests/test_conformance.py`** — a policy conformance matrix asserting, for every
  (principal, purpose) pair, exactly which sources are readable. Read it as the
  specification of who can see what. A policy edit that widens access fails here rather
  than in production.
- **`tests/test_redteam.py`** — authorization bypass, identity spoofing, purpose
  escalation, namespace escape, and capability replay, each written as an attack.

## Roadmap

- v2: tool-call governance on the same chokepoint — blast-radius estimation, approval
  escalation for irreversible actions, rollback
- IdP-backed principal registry (SCIM / Okta / Entra) with revocation propagation
- Pluggable embedding index behind the existing broker interface
- External anchoring for the lineage head hash

## License

Apache-2.0
