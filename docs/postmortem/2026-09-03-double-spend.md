# Eight threads, one approval, eight refunds

*A post-mortem on a double-spend race in an AI agent action gateway — and the two
further bugs that fixing it uncovered.*

**Date:** 2026-09-03
**Component:** Aperture action gateway
**Impact:** None in production. Found by a test written before the code had users.
**Severity if shipped:** Critical — duplicate financial transactions, and an audit
log that could not be trusted to tell you it happened.

---

## Summary

I built a gateway that lets AI agents take real actions — issue a refund, close a
ticket, delete accounts — with a human approval step in front of the dangerous
ones. The whole point is that one approval authorizes one action.

I gave it a single approved $50 refund and eight concurrent requests to execute it.

It issued eight refunds. $400.

```
proposal: prp_29262da029234d63 ready
executions returned: 8
refusals: []
REFUND ROWS ACTUALLY WRITTEN: [(1, 50.0), (2, 50.0), (3, 50.0), (4, 50.0),
                               (5, 50.0), (6, 50.0), (7, 50.0), (8, 50.0)]
TOTAL REFUNDED: 400.0

VERDICT: DOUBLE SPEND
```

Every guard in the gateway worked. Identity was checked. Policy was re-evaluated.
The approval was verified. The argument hash matched. The proposal's state was
`ready`, and the code checked that it was `ready`, eight times, correctly.

The bug is not in any of those checks. It is in the gap between checking and acting.

---

## What the system does

A one-paragraph model, enough to follow the bug.

An agent cannot act directly. It **proposes** an action. The gateway runs a dry
run against real state to measure the blast radius — how many records, how much
money, whether it can be undone — applies policy, and either refuses, marks the
proposal `ready`, or parks it as `pending_approval` for a human. A separate
**execute** call runs a `ready` proposal exactly once and records how to undo it.

Two calls, deliberately. The reviewer decides on one proposal; the execution
belongs to that decision.

---

## How it was found

Not by a user, and not by luck. I was about to add features to the action gateway
and decided to audit the store first, because it holds the state that decides
whether an approval has been spent. The audit was a script: propose one refund,
then hit `execute` from eight threads.

I expected one execution and seven refusals. I got eight executions.

The test that found it is now `tests/test_concurrency.py`, and it runs on every
push.

---

## The bug

State lived in a JSON file. Saving a proposal looked like this:

```python
def save_proposal(self, proposal: Proposal) -> Proposal:
    """Insert or update a proposal."""
    data = self._read(self.proposals_path)          # read the whole file
    data[proposal.id] = json.loads(proposal.model_dump_json())
    self._write(self.proposals_path, data)          # write the whole file
    return proposal
```

That is read-modify-write, and it is not the only one. `execute` did the same
thing at a larger scale:

```python
def execute(self, proposal_id, principal_id, ...):
    proposal = self.store.get_proposal(proposal_id)      # ← READ

    # ... identity checks, expiry, argument hash ...

    if proposal.state is ProposalState.EXECUTED:         # ← CHECK
        return self._refuse(Reason.ALREADY_EXECUTED, ...)

    # ... policy re-evaluation ...

    result, compensation = executor.execute(spec, proposal.arguments)   # ← ACT

    self.store.save_execution(record)
    self.store.save_proposal(                            # ← WRITE
        proposal.model_copy(update={"state": ProposalState.EXECUTED})
    )
```

Read. Check. Act. Write.

With one caller that is correct. With eight, the interleaving is:

```
T1  read → state="ready"  ✓ check passes
T2  read → state="ready"  ✓ check passes      (T1 has not written yet)
T3  read → state="ready"  ✓ check passes
...
T8  read → state="ready"  ✓ check passes
T1  ACT → refund #1
T2  ACT → refund #2
...
T8  ACT → refund #8
T1..T8  write state="executed"    (all eight, harmlessly, at the end)
```

Every thread observed a true fact — the proposal *was* `ready` when it looked —
and every thread was the first to look. The state check was a memory of the past
by the time it mattered.

This is time-of-check to time-of-use, and it is old and well documented. Knowing
the name did not stop me writing it, which is the honest and uncomfortable part.

### Why the existing tests were green

At the time, the suite had a test called `test_a_proposal_executes_only_once`:

```python
def test_a_proposal_executes_only_once(gateway):
    proposal = propose(gateway)
    assert isinstance(gateway.execute(proposal.id, "svc_support_agent"), ExecutionRecord)
    assert gateway.execute(proposal.id, "svc_support_agent").reason is Reason.ALREADY_EXECUTED
```

It passes. It has always passed. It tests exactly the thing that broke, and it is
incapable of detecting the break, because it calls `execute` twice in sequence and
the bug only exists when the calls overlap.

A green suite told me a property held. It actually told me the property held *in
the absence of concurrency*, which is a much weaker statement, and nothing in the
test's name said so.

---

## The fix

Move the decision into the storage engine, so that checking and acting are one
indivisible step.

State moved from JSON to SQLite in WAL mode, and execution now begins with a
claim:

```python
def transition(self, proposal_id, expected, new_state) -> bool:
    """Atomically move a proposal between states.

    Returns True only for the caller that actually performed the transition, so
    callers can use it as a mutual-exclusion primitive.
    """
    allowed = [str(state) for state in expected]
    placeholders = ",".join("?" * len(allowed))
    cursor = self._connect().execute(
        f"UPDATE proposals SET state = ? WHERE id = ? AND state IN ({placeholders})",
        (str(new_state), proposal_id, *allowed),
    )
    return cursor.rowcount == 1        # ← True for exactly one caller


def claim_for_execution(self, proposal_id: str) -> bool:
    return self.transition(proposal_id, [ProposalState.READY], ProposalState.EXECUTING)
```

The `WHERE state IN (...)` clause is the check. The `UPDATE` is the act. The
database serializes them, so `rowcount == 1` is true for precisely one caller and
false for everyone else.

In the gateway, the claim is the line that separates deciding from doing:

```python
# Atomically claim the proposal. Exactly one caller can win this.
# Everything above this line is a check; everything below it is a side effect.
if not self.store.claim_for_execution(proposal.id):
    return self._refuse(Reason.PROPOSAL_IN_FLIGHT, ...)

result, compensation = executor.execute(spec, proposal.arguments)
```

Same test, after:

```
proposal: prp_ba405f0c4fa04ded ready
executions returned: 1
refusals: [proposal_in_flight × 7]
REFUND ROWS ACTUALLY WRITTEN: [(1, 50.0)]
TOTAL REFUNDED: 50.0

VERDICT: safe
```

Rollback had the identical shape — read `rolled_back_at`, check it is null,
compensate, write — so it got the identical treatment. Two callers can no longer
both reverse one refund.

### The decision not to retry

`claim_for_execution` moves a proposal to `executing`. If the executor then dies —
the payment API times out, the process is killed — the proposal is stranded in
`executing` forever.

That is deliberate, and it was the hardest call in the fix.

The tempting thing is to release the claim on failure so the action can be retried.
But at the moment the executor raises, **the gateway does not know whether the
action took effect**. A payment API that times out may or may not have moved money.
Releasing the claim invites a retry that double-charges — the exact failure just
fixed, reintroduced through the error path. Holding the claim may strand a
half-finished operation.

Both are bad; only one is silent. So the proposal stays in `executing`, and
`aperture actions stuck` surfaces it for a human:

```
1 proposal(s) stranded mid-execution.
These are NOT retried automatically: the action's outcome is unknown,
so retrying may double-charge and abandoning may strand an operation.
```

A machine cannot choose correctly here. Saying so in the interface is better than
pretending it can.

---

## The second bug, found by the first fix

Once the concurrency tests existed, they were run against everything. One started
failing intermittently — roughly one run in three:

```
FAILED tests/test_concurrency.py::test_concurrent_rollback_reverses_once
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

The traceback pointed somewhere I did not expect:

```
src\aperture\actions\gateway.py:475
src\aperture\lineage.py:68
src\aperture\lineage.py:101
```

`lineage.py` is the audit log. Not the store — the log.

Appending an entry to a hash chain requires reading the current head:

```python
def append(self, payload):
    prev_hash, seq = self._head()               # ← read the last entry
    entry = {"seq": seq + 1, ..., "prev_hash": prev_hash}
    entry["hash"] = compute_hash(entry, prev_hash)
    with self.path.open("a") as handle:         # ← append
        handle.write(json.dumps(entry) + "\n")
```

Read, compute, write. The same shape as the first bug, in the component whose
entire job is to be trustworthy.

Two threads read the same head and chain two entries to the same predecessor: the
chain **forks**, and verification fails. Worse, a reader can catch a line
mid-write and get half a JSON object — which is what the `JSONDecodeError` was.

An audit log that corrupts under concurrent writes fails exactly when it is most
needed, because the incident you are investigating is usually the busy one.

The fix is a lock covering the read-compute-write, keyed by resolved path — not
held on the instance, because `Workspace.lineage` returns a fresh object each
access and per-instance locks would have protected nothing:

```python
_LOCKS: dict[str, threading.Lock] = {}

def _lock_for(path: Path) -> threading.Lock:
    """Return the shared append lock for a log file."""
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock
```

Plus two regression tests: eight threads writing six entries each, asserting
gapless sequence numbers and a verifying chain; and the realistic version, eight
agents taking actions at once, asserting the audit trail survives.

---

## The third bug, which was in a test

A different test started failing only in slow runs:

```python
statuses = [client.post("/api/actions/propose", json=payload).status_code
            for _ in range(ACTION_LIMIT + 5)]
assert statuses.count(200) == ACTION_LIMIT      # ← flaky
```

That asserts an exact request count against a rate limiter using a **real** 60
second clock. In a slow run the requests spanned the window, it rolled over, and
the limiter correctly allowed more through. The test failed; the code was right.

The fix was to make the clock injectable:

```python
def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
    self._clock = clock
```

Worth including because the temptation was to loosen the assertion to
`assert 429 in statuses` and move on. That would have kept the suite green while
deleting the only test that pinned the limit to a number. Flakiness is usually a
message about a hidden dependency — here, on wall-clock time — and the fix is to
remove the dependency, not the assertion.

---

## Does the fix hold across processes?

A lock protects one process. Production runs several workers.

The claim is not an in-process lock — it is a conditional `UPDATE` that SQLite
serializes through file locking — so it should hold across processes. "Should" is
not a test, so:

```python
def test_separate_processes_cannot_double_execute(workspace, tmp_path):
    """Threads share a lock; processes do not."""
    ...
    processes = [subprocess.Popen([sys.executable, script, ...]) for _ in range(4)]
    outputs = [p.communicate()[0].strip() for p in processes]

    assert outputs.count("EXECUTED") == 1, outputs
    assert ops_rows(workspace, "SELECT amount FROM refunds") == [(40.0,)]
```

Four real interpreters. One execution.

It does **not** hold across machines, because two hosts have two SQLite files and
both would win. So the store became an interface, with a Postgres implementation
for deployments that span machines, and both backends are held to the same
conformance suite rather than tested separately:

```python
@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    ...

def test_claim_admits_exactly_one_caller(store):
    """The double-spend guard, per backend."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_for_execution(proposal.id), range(8)))
    assert results.count(True) == 1
```

CI runs it against a real Postgres service container, with a further test racing
eight independent connections — the way separate machines would.

---

## What I took from it

**A green suite is a claim about the conditions you tested, not about the code.**
`test_a_proposal_executes_only_once` was true and useless. The name promised a
property; the body checked it under one condition. Where a property must hold
under concurrency, the test has to create concurrency — threads, and then
processes.

**Correctness lives at the boundary between checking and acting.** Every
individual guard here was right. The bug lived in the space between them. When
that space contains a side effect, the check and the act have to become one
operation, and the storage engine is usually the only thing that can make them one.

**Audit code is not exempt.** I would have said the log was the most carefully
written part of the project. It had the same bug, in a component whose only job is
to be believable afterwards.

**Error paths deserve the same scrutiny as happy paths.** The most dangerous
version of this bug was not the original — it was the tempting "fix" of releasing
the claim when an executor fails, which would have reintroduced double-spend
through the failure path, where nobody looks.

**Write the adversarial test before you need it.** This was found because I
audited the store before adding features rather than after shipping. The cost was
an afternoon. The cost of finding it in production is a customer's bank statement
and a conversation about whether your logs can be trusted.

---

## Reproduce it

The buggy version is in the history, so you can watch it fail:

```bash
git clone https://github.com/Medhovarsh/aperture
cd aperture && pip install -e ".[dev]"

# The fix, and the test that proves it
python -m pytest tests/test_concurrency.py -v

# The original bug, three commits back
git stash && git checkout 8abb1e2
# ... run the eight-thread script from this post ...
```

Code: <https://github.com/Medhovarsh/aperture> (Apache-2.0)
Live demo: <https://aperture-eight-bice.vercel.app>

The project is Aperture — a governance layer that sits between AI agents and the
data and systems they can reach. The action gateway described here is one half of
it; the other half governs what an agent is allowed to *see*.
