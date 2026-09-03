# Positioning

Internal document. The purpose is to make everything else — the README, the
landing page, a cold email, an interview answer — say the same thing.

## The one sentence

**Aperture is the chokepoint enterprise agents read and act through, so what an
agent sees and does is governed, measured, and provable.**

## The wedge, in the customer's words

> "We can't put the agent in front of real data because we can't prove what it saw.
> And we can't let it *do* anything, because there's no undo and no approval."

Every enterprise agent pilot stalls at one of those two sentences. Aperture is
built to be the answer to both, at the same chokepoint.

## Why now

1. Agent pilots moved from demo to procurement in 2025–2026, and procurement asks
   questions demos never had to answer.
2. The EU AI Act's record-keeping and human-oversight obligations turn "we log
   some things" into a specification with a date attached.
3. MCP became the de facto way agents reach tools, which means there is finally a
   standard place to put a chokepoint. Before MCP, this product would have needed
   a bespoke integration per runtime.

## Who buys, who uses

| | Champion | Buyer | Blocker |
|---|---|---|---|
| **Platform / AI infrastructure team** | Feels the pain first: they hand-rolled permission filtering and know it is thin | VP Engineering / Head of Platform | "Can we just add a WHERE clause?" |
| **Security engineering** | Asked to approve an agent deployment with no way to reason about it | CISO | "Is this another thing to run?" |
| **Data governance / privacy** | Owns purpose limitation and cannot currently evidence it for AI | DPO / Chief Data Officer | Wants a vendor with a compliance team |

Best first conversation: the platform team that has already built half of this
badly and knows it. They do not need to be convinced the problem exists.

## Message house

**Roof:** Agents can't be trusted with enterprise data until what they see and do
is governed at one chokepoint.

**Pillar 1 — Denial is explainable, never silent.**
Today an agent without permission silently gets a thinner context and answers
confidently anyway. Aperture returns the reason and makes the model disclose it.
*Proof:* the two-command demo, same question, two purposes.

**Pillar 2 — Actions are priced before they happen.**
`region=legacy` is one short string. It is also seven deleted accounts and
10,320 USD. The gap between those is what a human needs to see.
*Proof:* blast radius measured by dry run, refused on impact.

**Pillar 3 — The audit trail survives an auditor.**
Hash-chained, append-only, externally anchored, exportable to a SIEM. It detects
a wholesale rewrite, which a plain log cannot.
*Proof:* `aperture lineage verify` failing after a rewrite.

**Foundation — It installs where the data is.**
No model API key, no vector database, no cloud account, no paid dependency. It
runs inside a locked-down network because that is where the data it governs lives.

## Objection handling

**"Our vector DB already has metadata filters."**
Filters are silent. The agent never learns something was withheld, so it answers
from a partial context as though it were complete. That is not an access control
problem, it is a truthfulness problem, and it is the one that produces confidently
wrong answers to executives. Also: filters govern reads. They do nothing about
what the agent *does*.

**"We'll build this ourselves."**
Most teams have already built a third of it. The parts that take the longest are
not the obvious ones: atomic execution (we shipped a double-spend bug and the test
that catches it), rolling budgets, approval bound to an argument hash, and an
audit log that does not corrupt under concurrent writes. It is Apache-2.0 — fork it.

**"Isn't this what Guardrails AI / NeMo / LLM Guard do?"**
Those guard the prompt and the response. Aperture governs the retrieval and action
layer between them. Different position in the stack; they compose.

**"We need SSO, not a config file."**
RS256 against your JWKS endpoint. Okta, Entra, Auth0, Keycloak.

**"Who else runs this?"**
Nobody yet, and pretending otherwise would be the fastest way to lose a security
review. It is open source, the test suite runs 26 documented attacks in CI, and
the README lists what it does not do. Read the threat model and decide.

## Proof assets, ranked by what they actually convince

1. **The live playground** — the argument in twenty seconds, no install.
2. **`tests/test_conformance.py`** — who-sees-what as a specification that fails CI.
3. **The double-spend story** — a real bug, found by writing the test, with the
   before-and-after output. Engineers trust a project that shows its scars.
4. **"What is not claimed"** in the README — reads as maturity, not weakness.
5. **`docs/COMPLIANCE.md`** — the artifact that gets a security review unstuck.

## What we deliberately do not say

- No "enterprise-grade" without a specific mechanism next to it.
- No invented customers, logos, testimonials, or adoption numbers.
- No claim of compliance. We produce evidence; auditors decide.
- No "AI-powered" anything. The product's value is that the decisions are
  deterministic and reviewable.
