# Security Policy

Aperture is a security control. Its failure modes are somebody else's data breach,
so this page is specific about what is protected, what is not, and how to report a
problem.

## Reporting a vulnerability

Report privately through [GitHub Security Advisories](https://github.com/Medhovarsh/aperture/security/advisories/new).
Please do not open a public issue for anything that would let someone read data or
take an action they should not.

Include the version or commit, a description of the impact, and the smallest
reproduction you can manage. A failing test against the demo workspace is ideal,
because the demo already contains the identities and classifications an attack
usually needs.

Expect an acknowledgement within 3 working days and an assessment within 10. This
is a single-maintainer project; that is the honest cadence rather than an
enterprise SLA.

## What counts as a vulnerability

Anything that breaks one of these properties is in scope:

| Property | Broken if |
|---|---|
| Default deny | Access is granted with no matching allow rule |
| Fail closed | An error, crash, or malformed input results in access rather than denial |
| Read/action separation | Read permission yields the ability to take an action |
| Purpose binding | A caller reaches data outside the purpose they declared |
| Tenant isolation | A principal sees a record belonging to another tenant |
| Blast radius integrity | An agent influences the measured impact of its own action |
| Approval integrity | An action runs without a required approval, or with different arguments than were approved, or approved by its own proposer |
| Execute-once | One approval results in more than one execution |
| Audit integrity | An access or action occurs with no lineage entry, or the chain verifies after being altered |
| Identity integrity | A caller acts as a principal they were not issued, or replays an assertion |
| Secret hygiene | Retrieved content, action arguments, or tokens appear in logs or metrics |

Out of scope: the demo workspace's deliberately permissive fixtures, the hosted
playground's deliberate demo affordances (it lets you choose an identity and
approve actions on purpose — the real MCP server does neither), rate limits on the
public demo, and findings that require write access to the workspace directory,
which is already full control.

## Security properties, and how they are enforced

Each of these has a corresponding test in `tests/test_redteam.py` or
`tests/test_concurrency.py`.

- **Identity is never taken from model-influenced input.** The MCP server pins the
  principal at launch or verifies a signed assertion. Anything an agent can put in
  a tool argument, a prompt injection can put there too.
- **Purpose travels inside the signature.** A caller cannot present a token minted
  for one purpose and declare another.
- **Assertions are single use** and short lived; the `jti` is recorded.
- **The JWT algorithm is fixed by policy**, never read from the token header.
- **Policy evaluation is total.** Any exception denies with `policy_error`.
- **Missing metadata is restrictive.** A record with no ACL is withheld.
- **Executor faults are refusals.** Executors are treated as untrusted code at the
  boundary; any exception becomes a reason code.
- **Execution is an atomic claim**, holding across threads and processes.
- **A failed execution is parked, never retried.** The outcome is unknown, so a
  machine must not choose between double-charging and abandoning.
- **Approval is not reachable from the agent surface.** There is no approve tool.
- **The audit chain is append-only, serialized, and externally anchorable.**

## Known limitations

Stated plainly, because a control whose limits are undocumented gets deployed
outside them.

- **Checkpoints must be shipped off-box.** Aperture writes and verifies them.
  Anchoring to a secret readable by the host that writes the log proves nothing.
- **Single host.** SQLite makes execution atomic across processes on one machine.
  Several machines sharing one workspace need a shared transactional store.
- **HMAC assertions share a secret** between issuer and verifier. Use the `idp`
  extra and RS256 where public-key verification matters.
- **The plane governs what passes through it.** An engineer who can query the
  vector store directly bypasses it. Aperture is the chokepoint only if the raw
  sources are not otherwise reachable.
- **No protection against a malicious workspace.** Whoever can write `policy.yaml`
  controls the policy. Treat it as production configuration: reviewed, versioned,
  and access-controlled.

## Supported versions

Pre-1.0. Fixes land on `main`; there are no backported release branches yet.
