# Threat model

Written for a security reviewer deciding whether to let agents near real data.
It states what Aperture assumes, what it defends, and what it does not.

## System in one paragraph

Agents reach enterprise data and enterprise systems only through Aperture. Reads
pass identity, purpose, source eligibility, semantic routing, record-level
enforcement, and budgeting. Actions are proposed, priced against real state,
checked against policy, escalated to a human when required, executed under an
atomic claim, and recorded with a way to undo them. Every outcome, including every
refusal, lands in a hash-chained log.

## Trust boundaries

```
   UNTRUSTED                      SEMI-TRUSTED                 TRUSTED
   ─────────                      ────────────                 ───────
   Document content               Agent runtime                policy.yaml
   Retrieved records              MCP client                   catalog.yaml
   Tool arguments                 Executors (fault-isolated)   principals.yaml
   Model output                                                Signing secrets
   Playground visitors                                         The plane itself
```

The important line is between the agent runtime and the plane. The agent is
**semi-trusted**: it is expected to call correctly, but never to be honest about
who it is or what its action would do. Every security decision is computed on the
plane's side of that line from inputs the agent cannot influence.

## Adversaries

**A1 — Prompt injection in content.** An attacker who can get text into an indexed
source. The realistic one: a document, a ticket, a wiki page, an email.

**A2 — A compromised or misbehaving agent.** Correct protocol, hostile or looping
behavior. Includes an agent stuck in a retry loop, which is the common case and
does the same damage as malice.

**A3 — A curious insider.** A legitimate principal reaching for data outside their
role or purpose.

**A4 — A compromised executor endpoint.** The external system an action calls has
been taken over or misconfigured.

**A5 — An attacker with host write access.** Can edit the audit log and workspace
files. Mostly out of scope; noted where anchoring helps.

## Attacks and mitigations

| # | Attack | Adversary | Mitigation | Test |
|---|---|---|---|---|
| 1 | Poisoned document instructs the system to widen access | A1 | Authorization is computed from principal and policy; content never participates | `test_injected_instructions_in_a_document_do_not_widen_access` |
| 2 | Agent claims a different identity in a tool argument | A2 | Identity is pinned at launch or proved by signed assertion | `test_principal_cannot_be_overridden_through_tool_arguments` |
| 3 | Captured assertion replayed | A2 | Single use; `jti` recorded and durable across restarts | `test_assertion_is_single_use` |
| 4 | JWT algorithm confusion (`alg: HS256` signed with the public key) | A2 | Algorithm fixed by policy, never read from the token | `test_algorithm_confusion_is_rejected` |
| 5 | Token from another tenant or another service | A2 | Issuer and audience verified | `test_wrong_issuer_is_rejected`, `test_wrong_audience_is_rejected` |
| 6 | Declaring a purpose that was not granted | A3 | Purpose is inside the signature; source and action allowlists are enforced | `test_purpose_binding_holds_for_the_same_identity` |
| 7 | Reading another tenant's records | A3 | Tenant is the first gate, unconditional | `test_cross_tenant_record_cannot_be_fetched_by_id` |
| 8 | Reusing a record id seen earlier | A3 | Ids are not capabilities; permissions re-evaluated per fetch | `test_a_record_id_is_not_a_durable_capability` |
| 9 | Enumerating the catalog to map the organization | A3 | Sources the caller cannot read are omitted, not marked denied | `test_source_listing_hides_sources_the_caller_cannot_read` |
| 10 | Accumulating read access into action authority | A2, A3 | Action rules and read rules are disjoint rule sets | `test_read_access_never_becomes_action_authority` |
| 11 | Agent approves its own action | A2 | Self-approval refused; no approve tool on the MCP surface | `test_an_agent_cannot_approve_its_own_proposal` |
| 12 | Approve small, execute large | A2 | Approval bound to an argument hash | `test_approval_is_bound_to_the_arguments_that_were_reviewed` |
| 13 | Under-reporting an action's impact | A2 | Blast radius measured by a dry run against real state | `test_blast_radius_comes_from_state_not_from_the_agent` |
| 14 | Many individually-legal actions | A2 | Rolling-window spend and rate ceilings, re-checked at execution | `test_spend_budget_stops_a_retry_loop` |
| 15 | Racing one approval into several executions | A2 | Atomic claim; holds across processes | `test_concurrent_execute_issues_exactly_one_refund` |
| 16 | Using an approval after permission is revoked | A2, A3 | Policy re-evaluated at execution; approvals expire | `test_permission_revoked_after_approval_stops_execution` |
| 17 | Catalog path used to read arbitrary files | A5 | Source paths confined to the workspace | `test_catalog_path_cannot_escape_the_workspace` |
| 18 | SQL injection through catalog identifiers | A5 | Identifiers validated against the live schema | `test_sql_identifiers_come_from_the_catalog_and_are_validated` |
| 19 | Action endpoint redirects to cloud metadata (SSRF) | A4 | Redirects refused outright | `test_redirects_are_refused` |
| 20 | Action credentials sent in the clear | A4 | Plaintext HTTP refused for non-local hosts | `test_plaintext_http_to_a_remote_host_is_refused` |
| 21 | Endpoint claims an undo it cannot perform | A4 | Reversibility verified at catalog load and at execution | `test_catalog_rejects_a_false_reversibility_claim` |
| 22 | Hung endpoint holds a claim open | A4 | Mandatory timeouts; stranded proposals surfaced to a human | `test_timeouts_are_enforced` |
| 23 | Editing the audit log | A5 | Hash chain; verification names the altered entry | `test_verify_detects_edited_content` |
| 24 | Rewriting the audit log wholesale | A5 | Signed checkpoints anchored off-box | `test_checkpoint_detects_a_wholesale_rewrite` |
| 25 | Corrupting the chain through concurrent writes | A2 | Appends serialized per file | `test_lineage_chain_survives_concurrent_writes` |
| 26 | Exfiltrating data through logs or metrics | A5 | Content, arguments, and tokens never logged; metric labels bounded | `test_sensitive_fields_never_reach_the_log` |

## Residual risk

**The model still sees what it is allowed to see.** Aperture decides what reaches
the context window. It cannot stop an authorized agent from relaying authorized
data to an unauthorized person. That is a DLP problem downstream.

**A determined insider with host access wins locally.** Anchoring makes tampering
detectable after the fact, not impossible.

**Semantic routing is a quality control, not a security control.** A poorly worded
source description sends a question to the wrong place; it never sends it to a
source policy forbids.

**Availability is not addressed.** There is no defense against someone with
credentials issuing expensive queries in a loop beyond the rate and budget
ceilings, which are configured per deployment.

## Assumptions

1. The workspace files are trusted configuration under change control.
2. Signing and anchoring secrets are held outside the machine that writes the log.
3. Raw sources are not otherwise reachable by the agent. A chokepoint with a way
   around it is a suggestion.
4. The MCP transport is a local subprocess pipe, or TLS if adapted to a network.
5. Executors are fault-isolated but not sandboxed: a malicious executor runs with
   the plane's privileges. Register only executors you would run yourself.
