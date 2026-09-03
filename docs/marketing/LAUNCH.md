# Launch drafts

Every asset below is a **draft for you to post under your own name**. Nothing here
has been published. Read each one before sending; the voice should be yours.

Two rules baked into every draft, because breaking either is how a launch turns
into a liability:

1. **No invented traction.** No customers, no logos, no "trusted by", no download
   counts. You have none yet, and the first person to check will be the one you
   most wanted to impress.
2. **The limits are stated in the post itself.** Engineers reward a project that
   says what it does not do. Hiding it only delays the discovery by one comment.

---

## Show HN

**Title** (80 char limit, no "Show HN:" needed in the field on some forms):

```
Show HN: Aperture – a governed context plane so AI agents can't silently over-read
```

Alternatives if that one reads long:
- `Show HN: Aperture – purpose-bound, auditable data access for AI agents`
- `Show HN: I found a double-spend bug in my own agent action gateway, then fixed it`

**Body:**

```
Every enterprise agent deployment I looked at rebuilds the same broken plumbing:
the vector index has no idea who's asking, so permission filtering — where it
exists at all — is a silent WHERE clause. The agent never learns results were
withheld, so it answers confidently from a partial context.

Aperture is a chokepoint agents read and act through. It knows four things at
once: what the data means, who may see it, how fresh it is, and how to explain a
refusal. It runs as an MCP server, so any MCP-speaking runtime integrates in one
config line.

The demo that makes the point in 20 seconds — same corpus, same question, two
different declared purposes:

  aperture query -p u_dana --purpose hr_support "how much parental leave?"
  → 1 record from hr_handbook

  aperture query -p u_kim --purpose customer_support "how much parental leave?"
  → 0 records | 3 withheld (purpose_not_permitted)

The support agent doesn't quietly get a thin answer. It gets told what it couldn't
see, and the model is instructed to disclose it.

v2 extends the same chokepoint to actions: propose → measure blast radius against
real state → policy → human approval if required → execute → undo. One short
argument `region=legacy` turns out to mean seven deleted accounts and $10,320, and
the gap between those two facts is what a reviewer needs.

Two things I'd rather you hear from me than find yourself:

1. I shipped a double-spend bug. Eight concurrent execute() calls on one approved
   $50 refund issued eight refunds, because the store did read-modify-write. It's
   now SQLite with an atomic claim, and tests/test_concurrency.py runs the original
   attack. The audit log had the same class of bug — concurrent appends forked the
   hash chain — also fixed, also tested.

2. The README has a "What is not claimed" section: single host, HMAC assertions
   unless you use the IdP extra, demo executors, lexical BM25 retrieval.

Live demo (synthetic data, isolated per visitor): https://aperture-eight-bice.vercel.app
Code, Apache-2.0: https://github.com/Medhovarsh/aperture

No paid dependencies, no model API key, no vector DB required — it has to install
inside a locked-down network, because that's where the data it governs lives.

Happy to be told the design is wrong.
```

**Timing:** Tuesday–Thursday, 08:00–10:00 ET. Post and then stay at the keyboard
for four hours; the first hour of comments decides the thread.

**Comment prep** — have these ready, they will be asked:
- *"How is this different from Guardrails AI?"* → different layer; they do prompt
  and response, this does retrieval and action. Point at `docs/COMPARISON.md`.
- *"Why not OPA/Cedar?"* → they evaluate a decision you hand them; they don't
  measure blast radius, route questions, or hold approvals. The `Policy` interface
  is small enough to swap.
- *"BM25? In 2026?"* → deliberate: no model download, installs air-gapped. The
  broker interface takes an embedding index without touching anything else.
- *"Who's using it?"* → nobody yet. Say it plainly.

---

## LinkedIn

```
I shipped a bug that refunded a customer eight times, then wrote the test that
catches it.

I've been building Aperture — a governance layer that sits between AI agents and
enterprise data and systems. While hardening the action gateway I ran eight
concurrent execute() calls against a single approved $50 refund.

Result: eight refunds. $400.

The store did read-modify-write, so every thread passed the "is this already
executed?" check before any of them wrote back. A single-threaded test suite will
never find that. Production finds it on the day it hurts most.

The fix: execution now begins with an atomic claim that exactly one caller wins.
The other seven are told the proposal is already in flight. Then the same class of
bug turned up in the audit log — concurrent appends forked the hash chain — so
appends are serialized too.

The wider point, and the reason I'm building this:

Agent pilots stall at two sentences. "We can't prove what it saw." And "there's no
undo." Those aren't model problems. They're plumbing problems, and every company
is solving them badly and separately.

Aperture makes both auditable at one chokepoint: purpose-bound access with
explainable refusals, and actions that are priced before they happen — blast
radius measured against real state, human approval for the irreversible ones,
rollback for the rest.

Open source, Apache-2.0. Live demo and code in the comments.

If you're running agents against real company data: how are you answering those
two sentences today? Genuinely asking — I want to know where this is wrong.
```

*(Put links in the first comment, not the post. LinkedIn suppresses posts with
outbound links.)*

---

## X / Twitter thread

```
1/ I gave my AI agent gateway one approved $50 refund and eight concurrent
   requests to execute it.

   It issued eight refunds. $400.

   Here's the bug, the fix, and why I think it matters more than the feature I
   was building 🧵

2/ The store did read-modify-write. Every thread read "state: ready", passed the
   check, and wrote back.

   Classic. Invisible to a single-threaded test suite. Expensive in production,
   because the thing being duplicated is money.

3/ Fix: execution starts with an atomic claim — one conditional UPDATE moving
   ready → executing that exactly one caller can win.

   8 concurrent calls → 1 execution, 7 × proposal_in_flight.

4/ Then the same class of bug showed up in the audit log.

   Appending reads the chain head, chains to it, writes. Unserialized, two threads
   chain to the same predecessor and the hash chain forks.

   An audit log that corrupts under load is worthless exactly when you need it.

5/ What I'm actually building: Aperture, a chokepoint agents read and act through.

   Same question, same corpus, different declared purpose → different answer. And
   the agent is TOLD what it couldn't see, instead of quietly answering from a
   thinner context.

6/ For actions: propose → measure blast radius against real state → policy →
   human approval → execute → undo.

   "region=legacy" is one short string. It's also 7 deleted accounts and $10,320.
   That gap is the entire product.

7/ Blast radius is measured, never asserted by the agent. A model that
   under-reports the damage of its own action must not be able to talk its way
   past a limit.

8/ Open source, Apache-2.0, no paid dependencies, no model API key.

   Live demo: https://aperture-eight-bice.vercel.app
   Code: https://github.com/Medhovarsh/aperture

   Tell me where it's wrong.
```

---

## Reddit — r/LocalLLaMA, r/mcp, r/devops

Reddit punishes anything that reads like marketing. Lead with the technical
artifact and let people find the project.

**Title:** `Found a double-spend race in my own agent action gateway — writeup and fix`

**Body:** the HN body, minus the last two lines, with the demo link at the bottom
rather than the middle. Answer every comment. Do not cross-post the same text to
several subreddits on the same day.

---

## Publishing checklist

- [ ] Live demo is up (`curl https://aperture-eight-bice.vercel.app/readyz`)
- [ ] CI badge is green on `main`
- [ ] README opens with the demo image and the live link
- [ ] `docs/COMPLIANCE.md` and `docs/THREAT_MODEL.md` are linked from the README
- [ ] You have four uninterrupted hours after posting
- [ ] You are ready to answer "who uses it?" with "nobody yet"
