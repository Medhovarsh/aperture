# Contributing

Aperture is a security control, so the bar for changes is different from a normal
library. This page says what that bar is.

## The rule that matters

**Every change that touches an authorization decision needs a test that fails
without it.** Not a test that passes with it — one that demonstrably fails when the
change is reverted. If the change closes an attack, write the attack.

That is why `tests/test_redteam.py` reads as a list of attacks and
`tests/test_concurrency.py` spawns real threads and real processes: both exist
because a bug got through a suite that looked green.

## Getting set up

```bash
pip install -e ".[dev,web,idp]"
python -m pytest -q
aperture demo --path workspace
bash examples/walkthrough.sh
```

## What CI enforces

| Job | What it proves |
|---|---|
| tests | The suite on Python 3.10–3.13, plus Windows and macOS |
| acceptance walkthrough | Purpose binding and action governance hold through the CLI |
| MCP stdio handshake | The tool surface is exactly what is expected, and approval is never exposed |
| production controls | Budget ceilings, single-use assertions, and rewrite detection |
| container image | The image builds, runs as non-root, and serves both surfaces |

## The invariants

Changing any of these is a design discussion before it is a pull request.

1. **Default deny.** No matching allow rule means no access.
2. **Fail closed.** Any error denies. Never propagate an exception past a
   policy check or an executor boundary.
3. **Read and action authority stay disjoint.** A rule naming actions governs only
   actions, and vice versa.
4. **Nothing is withheld silently.** Every drop carries a reason code.
5. **Identity never comes from model-influenced input.**
6. **Blast radius is measured, never asserted by the caller.**
7. **Approval is not reachable from the agent surface.**
8. **Every outcome is logged, including refusals, before the caller sees it.**

## Style

Match the surrounding code. Two specifics that matter more here than usual:

- **Comments explain why, not what.** The code says what it does. A comment earns
  its place by recording the reasoning or the attack that shaped it.
- **Docstrings on anything with a security consequence** say what happens when it
  fails, not only what it does when it works.

## Adding a connector or an executor

Both are single-method interfaces on purpose.

- **Brokers** (`src/aperture/brokers/`) do no authorization. They carry the source
  system's metadata faithfully and leave `acl` as `None` when they cannot determine
  it — the pipeline treats that as most-restrictive, so guessing is always worse
  than admitting ignorance.
- **Executors** (`src/aperture/actions/`) do no authorization either. `estimate`
  must not change anything. If you set `reversible = True`, `compensate` has to
  work, and the catalog verifies that claim at load time.

## Reporting a vulnerability

Do not open a public issue. See [SECURITY.md](SECURITY.md).
