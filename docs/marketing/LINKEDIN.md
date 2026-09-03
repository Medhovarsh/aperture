# LinkedIn post

**Draft for you to post under your own name. Nothing here has been published.**

Read it once and cut anything that doesn't sound like you. A post that reads as
written-by-someone-else performs worse than a rougher one in your own voice.

---

## The post — copy from here

```
I gave my AI agent gateway one approved $50 refund and eight concurrent requests
to execute it.

It issued eight refunds. $400.

Here's the bug, the fix, and the two further bugs that finding it uncovered.

━━━ WHAT THE SYSTEM DOES ━━━

I've been building Aperture, an open-source governance layer that sits between AI
agents and the data and systems they can reach.

For actions, an agent can't just do things. It proposes. The gateway measures the
blast radius against real state, applies policy, and either refuses or requires a
named human to approve. Then a separate call executes it once.

One approval authorizes one action. That's the entire contract.

━━━ THE BUG ━━━

State lived in a JSON file, and execute() did this:

  1. READ the proposal
  2. CHECK state != executed
  3. ACT — call the payment API
  4. WRITE state = executed

Correct with one caller. With eight, all eight read "ready", all eight passed the
check, and all eight acted — because nobody had written back yet.

Every guard worked. Identity was verified. Policy was re-evaluated. The approval
was checked. The argument hash matched. The state check ran eight times and was
correct eight times.

The bug wasn't in any check. It was in the gap between checking and acting.

Time-of-check to time-of-use. Textbook, decades old. Knowing the name didn't stop
me writing it, which is the uncomfortable part.

━━━ WHY MY TESTS WERE GREEN ━━━

The suite had this:

  def test_a_proposal_executes_only_once(gateway):
      assert execute(proposal) is ExecutionRecord
      assert execute(proposal).reason is ALREADY_EXECUTED

It passes. It always passed. It tests exactly the thing that broke — and it is
structurally incapable of catching it, because it calls execute twice in sequence
and the bug only exists when the calls overlap.

A green suite told me a property held. It actually told me the property held in
the absence of concurrency. Nothing in the test's name said so.

━━━ THE FIX ━━━

Move the decision into the storage engine so checking and acting become one
indivisible step. State went to SQLite, and execution now starts with a claim:

  UPDATE proposals SET state = 'executing'
  WHERE id = ? AND state = 'ready'

  → rowcount == 1 for exactly one caller

The WHERE clause is the check. The UPDATE is the act. The database serializes
them. Eight concurrent calls now produce one execution and seven refusals.

━━━ BUG #2: THE AUDIT LOG HAD IT TOO ━━━

Once the concurrency tests existed, I ran them against everything. One started
failing about one run in three, and the traceback pointed at the audit log.

Appending to a hash chain means reading the current head, linking the new entry to
it, and writing. Read, compute, write — the same shape as the first bug, in the
component whose only job is to be believable afterwards.

Two threads read the same head and chain to the same predecessor. The chain forks.
A reader catches a line mid-write and gets half a JSON object.

An audit log that corrupts under concurrent writes fails exactly when you need it,
because the incident you're investigating is the busy one.

━━━ BUG #3: THE MOST DANGEROUS ONE WAS THE FIX I ALMOST WROTE ━━━

When an executor fails mid-action, the tempting move is to release the claim so it
can be retried.

Don't. At the moment a payment API times out, you don't know whether the money
moved. Releasing the claim invites a retry that double-charges — the exact bug you
just fixed, reintroduced through the error path where nobody looks.

So a stranded action stays stranded, and a human is told:

  1 proposal(s) stranded mid-execution.
  These are NOT retried automatically: the action's outcome is unknown,
  so retrying may double-charge and abandoning may strand an operation.

A machine can't choose correctly there. Saying so in the interface beats
pretending it can.

━━━ WHAT I TOOK FROM IT ━━━

→ A green suite is a claim about the conditions you tested, not about your code.
  If a property must hold under concurrency, the test has to create concurrency.
  Threads, then processes. (I now spawn four real interpreters to prove the claim
  survives process boundaries.)

→ Correctness lives at the boundary between checking and acting. Every individual
  guard was right. The bug lived in the space between them.

→ Audit code isn't exempt. I'd have called the log the most carefully written part
  of the project. Same bug.

→ Error paths deserve the same scrutiny as happy paths.

━━━ WHY THIS MATTERS BEYOND MY PROJECT ━━━

Every enterprise agent pilot I've looked at stalls on two sentences:

  "We can't prove what it saw."
  "There's no undo."

Neither is a model problem. Both are plumbing problems, and every company is
solving them separately, in a hurry, with the same class of bug I just described.

What Aperture does about it:

• Access is bound to a declared purpose — same person, same question, different
  purpose, different answer
• When something is withheld the agent is TOLD, with a reason code. Today it just
  gets fewer results and answers confidently from a partial context
• Actions are priced before they run. "region=legacy" is one short string. It's
  also 7 deleted accounts and $10,320. That gap is the whole product
• Irreversible actions need a named human — and the agent has no approve button,
  because anything an agent can call, a prompt injection can call
• Every read, action and refusal is one hash-chained line, exportable to a SIEM,
  with signed checkpoints that detect a wholesale rewrite, not just an edit

Apache-2.0. No paid dependencies, no model API key — it has to install inside a
locked-down network, because that's where the data it governs lives.

Nobody is using it yet. I'd rather say that than pretend otherwise.

If you're putting agents in front of real company data: how are you answering
those two sentences today? I genuinely want to know where this design is wrong.

Full write-up, live demo and code in the comments.
```

---

## First comment (post this immediately after)

LinkedIn suppresses reach on posts with outbound links in the body, so links go
here.

```
Full technical post-mortem — the interleaving diagram, the before/after code, and
why the retry "fix" is worse than the bug:
https://github.com/Medhovarsh/aperture/blob/main/docs/postmortem/2026-09-03-double-spend.md

Live demo, synthetic data, nothing to install:
https://aperture-eight-bice.vercel.app

Code and threat model (26 attacks, each mapped to the test that covers it):
https://github.com/Medhovarsh/aperture

The concurrency tests are in tests/test_concurrency.py. They spawn real threads
and real processes, because this class of bug is invisible to anything else.
```

---

## A shorter variant

If the long one feels like too much for your feed, cut everything from "BUG #2"
through "WHAT I TOOK FROM IT", keep the first two lessons, and end on the same
question. The bug, the fix, and the two-sentences framing are what carry it.

---

## Notes on posting

**Timing.** Tuesday–Thursday, 08:00–10:00 in your audience's timezone.

**The first two lines are everything.** LinkedIn truncates at roughly three lines
behind a "see more". "It issued eight refunds. $400." has to be above that fold.

**Formatting.** The `━━━` dividers survive LinkedIn's stripping of markdown; `#`
and `**bold**` do not. Keep paragraphs short. Blank line between every idea.

**Engagement.** Reply to every comment in the first two hours — the algorithm
weights early velocity, and more importantly the comments are where the design
partners are.

**Hashtags.** Two or three at most: #AI #Security #OpenSource.

**Don't.** No "thoughts?" sign-off. No "Unpopular opinion:" opener. No claiming
users you don't have. No invented urgency.

### Comments to have answers ready for

**"Why not just use OPA/Cedar?"**
They evaluate a decision you hand them, and they're better at that than my rule
engine. They don't measure blast radius, route a question to a source, or hold an
approval. The Policy interface is small enough to swap one in.

**"Isn't this what Guardrails AI / NeMo do?"**
Different layer. They validate the prompt and the response. This governs the
retrieval and action layer between them. They compose.

**"Why is your database a JSON file / SQLite?"**
It was a JSON file, and that's the bug in the post. It's SQLite now, with a
Postgres backend for multi-host — both held to the same conformance suite,
including a race across eight independent connections.

**"Who's using it?"**
Nobody yet. Say it plainly; the post already does.

---

## The follow-up

If this performs, the next post is **not** another announcement. It's the
adversarial-testing angle: "your test suite is green and it's lying to you",
using `test_a_proposal_executes_only_once` as the example. Same audience, no
product pitch, and it earns the right to mention the project again.
