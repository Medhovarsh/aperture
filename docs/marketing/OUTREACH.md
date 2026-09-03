# Reaching companies

Drafts for you to send under your own name. Nothing here has been sent.

The goal of every message below is **one 25-minute conversation**, not a sale.
You are looking for design partners: teams already stuck on this, willing to tell
you what is wrong with your answer.

## Who to approach, in priority order

1. **Companies whose engineers have publicly complained about this.** Search for
   conference talks, blog posts, or HN comments about permission-aware RAG,
   agent approval workflows, or "we built a permissions layer for our copilot".
   Someone who wrote 2,000 words about the problem will read 120 about a solution.
2. **Regulated mid-market running agent pilots.** Fintech, health tech, insurance,
   HR tech. Big enough to have a compliance function, small enough that one
   platform lead can decide to try something.
3. **AI platform teams at companies with an internal copilot.** They have already
   built a third of this and know where it is thin.

Deprioritise: enterprises with a procurement process that requires a vendor with
SOC 2. You cannot serve them yet, and pretending otherwise wastes both parties'
time.

## Cold email — platform / AI engineering lead

Subject lines, pick one:
- `the silent WHERE clause problem in your copilot`
- `question about how <company> handles permissions in RAG`

```
Hi <name>,

You mentioned <specific thing they said publicly — talk, post, comment> about
<their agent/copilot>. That's why I'm writing rather than guessing.

A pattern I keep finding: permission filtering in RAG is a silent WHERE clause.
The agent never learns results were withheld, so it answers confidently from a
partial context. The user gets a wrong answer that looks like a complete one.

I built an open-source layer that makes the refusal explicit — same corpus, same
question, different declared purpose, and the agent is told what it couldn't see
and why. It also governs actions: measures the blast radius before anything runs,
escalates the irreversible ones to a named human.

Apache-2.0, no paid dependencies: https://github.com/Medhovarsh/aperture
20-second demo, nothing to install: https://aperture-eight-bice.vercel.app

Not selling anything — it's free and I'm the only maintainer. I want 25 minutes
with someone who's hit this in production to hear where the design is wrong.

Worth a conversation?

<your name>
```

**Why this shape:** one specific reference so it cannot be a mail merge, one
concrete problem statement, one link that needs no install, an explicit statement
that nothing is being sold, and one question. Under 150 words.

## Cold email — security / governance lead

Subject: `evidence for AI Act Article 12 from the retrieval layer`

```
Hi <name>,

If <company> is putting agents in front of internal data, you likely own the
question of what they're allowed to see and how you'd prove it afterwards.

I've open-sourced the layer I wanted to exist: purpose-bound access with
machine-readable refusal reasons, human approval for irreversible actions, and a
hash-chained access log with signed checkpoints that detect a wholesale rewrite —
not just an edit.

There's a control mapping to AI Act Articles 12 and 14, GDPR Article 5(1)(b),
NIST AI RMF, and ISO 42001 here — written to say what it does *not* cover as
plainly as what it does:
https://github.com/Medhovarsh/aperture/blob/main/docs/COMPLIANCE.md

Threat model, including residual risk:
https://github.com/Medhovarsh/aperture/blob/main/docs/THREAT_MODEL.md

Apache-2.0, single maintainer, no vendor behind it — say so to anyone who asks.
Would 25 minutes of your review be worth an outside perspective on your agent
data-access design?

<your name>
```

## Design-partner conversation guide

Ask, do not pitch. You learn more in 25 minutes of their answers than in an hour
of your slides.

1. Are agents touching real internal data today, or is that blocked? What blocked it?
2. How does an agent decide what it may see right now?
3. When it can't see something, what happens? *(This is the question. Most people
   pause here, because the answer is "nothing, it just gets fewer results".)*
4. Has an agent ever done something you had to undo? What did undoing involve?
5. If your auditor asked what an agent accessed last Tuesday, where would you look?
6. Who signs off before an agent does something irreversible?

Close with: *"If I could hand you one thing next month, which of these would it be?"*
Then build that.

## What not to do

- **No fabricated social proof.** No "used by teams at…", no invented case study,
  no logo wall. In this market, one exaggeration ends the conversation and the
  relationship.
- **No compliance claims.** You produce evidence. Auditors decide. The word
  "compliant" should never appear next to the product name.
- **No mass sending.** Twenty researched emails beat two thousand merged ones, and
  bulk unsolicited mail creates legal exposure under GDPR and CAN-SPAM. Check the
  rules for the jurisdiction you are mailing into before you send anything.
- **No pretending to be a company.** You are one maintainer with a good artifact.
  That is a fine thing to be, and it is checkable.

## Where else this reaches people

| Channel | Why it works | Effort |
|---|---|---|
| MCP server directories and awesome-lists | People actively shopping for MCP servers | Low |
| A talk at a local meetup on the double-spend bug | The bug is the hook; the product is the context | Medium |
| A written post-mortem of the race condition | Ages well, gets found by search, demonstrates judgment | Medium |
| Answering "permission-aware RAG" questions on Stack Overflow / Discord | Meets people at the moment they have the problem | Ongoing |
| Submitting to the AI Act / governance newsletters | Their readers are the security buyer | Low |

The written post-mortem is the highest-leverage single asset. A concrete bug, a
concrete fix, and a test that proves it is the most credible thing an unknown
maintainer can publish.
