# LinkedIn post

**Draft for you to post under your own name. Nothing here has been published.**

Read it once before posting and cut anything that doesn't sound like you. A post
that reads as written-by-someone-else performs worse than a rougher one in your
own voice.

---

## Recommended version — the bug story

Leads with a concrete failure. On LinkedIn, engineers share posts that admit
something; they scroll past posts that announce something.

```
I shipped a bug that refunded a customer eight times.

I've been building an open-source governance layer that sits between AI agents and
the systems they can actually change. While hardening it, I ran a test I'd been
putting off: eight concurrent requests to execute one approved $50 refund.

It issued eight refunds. $400.

The store did read-modify-write. Every thread read "state: ready", passed the
"has this already run?" check, and wrote back. All eight believed they were first.

A single-threaded test suite will never find that. Production finds it on the
worst possible day, and the thing being duplicated is money.

The fix: execution now starts with an atomic claim — one conditional UPDATE moving
ready → executing that exactly one caller can win. Eight concurrent calls now
produce one execution and seven refusals.

Then the same class of bug turned up in the audit log. Appending reads the chain
head, links the new entry to it, and writes. Unserialized, two threads link to the
same predecessor and the hash chain forks. An audit log that corrupts under
concurrent load is worthless precisely when you need it most.

Here's why I think this matters beyond my own project.

Every enterprise agent pilot I've looked at stalls on two sentences:

"We can't prove what it saw."
"There's no undo."

Neither is a model problem. Both are plumbing problems — and every company is
solving them separately, badly, in a hurry.

So I built Aperture: one chokepoint agents read and act through.

→ Access is bound to a declared purpose. Same person, same question, different
purpose, different answer.
→ When something is withheld, the agent is TOLD, with a reason code. Today it just
gets fewer results and answers confidently from a partial context.
→ Actions are priced before they run. "region=legacy" is one short string. It's
also 7 deleted accounts and $10,320. That gap is the whole product.
→ Irreversible actions need a named human. The agent has no approve button —
anything an agent can call, a prompt injection can call.
→ Every read, every action, every refusal is one hash-chained line you can export
to a SIEM.

Apache-2.0. No paid dependencies, no model API key. It has to install inside a
locked-down network, because that's where the data it governs lives.

Nobody is using it yet. I'd rather say that than pretend otherwise.

If you're putting agents in front of real company data: how are you answering
those two sentences today? I want to know where this design is wrong.

Live demo and code in the comments.
```

**First comment** (LinkedIn suppresses posts with outbound links in the body):

```
Live demo, synthetic data, nothing to install:
https://aperture-eight-bice.vercel.app

Code and threat model:
https://github.com/Medhovarsh/aperture

The concurrency tests that catch the bug above are in
tests/test_concurrency.py — they spawn real threads and real processes,
because that class of bug is invisible to anything else.
```

---

## Alternative version — the compliance angle

Use this one if your network is more security and governance than engineering.
Same project, different door.

```
The EU AI Act asks high-risk AI systems to keep automatic records of what they
did, and to give humans a real way to intervene.

Most agent stacks I've seen can't answer either. Not because teams don't care —
because the plumbing was never built. The retrieval layer doesn't know who's
asking, and the action layer has no approval step to intervene at.

I open-sourced the layer I wanted to exist.

For reads: access is bound to a declared purpose, and when something is withheld
the agent is told, with a machine-readable reason. Purpose limitation is GDPR
Article 5(1)(b); I've never seen it implemented natively in an agent stack.

For actions: the blast radius is measured against real state before anything runs
— records affected, money moved, external recipients, whether it can be undone —
and irreversible ones require a named human who is not the agent that proposed it.

For evidence: every read, action, and refusal is a hash-chained line. Signed
checkpoints detect a wholesale rewrite, not just an edit, which a plain log
cannot. Export is newline-delimited JSON straight into a SIEM.

There's a control mapping to AI Act Articles 12 and 14, GDPR, NIST AI RMF, ISO
42001 and SOC 2. It's deliberately written to say what it does NOT cover as
plainly as what it does — no tool makes an organisation compliant, and anyone
claiming otherwise is selling something.

Apache-2.0, single maintainer, no vendor behind it.

If you own AI governance somewhere: what's your current answer when an auditor
asks what an agent accessed last Tuesday?
```

---

## Notes on posting

**Timing.** Tuesday–Thursday, 08:00–10:00 in your audience's timezone.

**Format.** Short paragraphs, one idea each. LinkedIn truncates at roughly 3
lines, so the first two must earn the click — that's why the bug version opens
with the failure and the dollar figure.

**Engagement.** Reply to every comment in the first two hours. The algorithm
weights early comment velocity, and more importantly the comments are where you
find the design partners.

**Do not.** No hashtag spam (2–3 maximum: #AI #Security #OpenSource). No "thoughts?"
sign-off. No engagement-bait opener like "Unpopular opinion:". No claiming users
you don't have.

**Expect this comment:** *"Why not just use OPA/Cedar?"* Answer: they evaluate a
decision you hand them, and they're better at that than my rule engine. They don't
measure blast radius, route a question to a source, or hold an approval. The
`Policy` interface is small enough to swap one in.

**If it does well,** the follow-up post is the technical post-mortem of the race
condition — same story, more depth, and it ages into something people find by
search months later.
