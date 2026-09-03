# Control mapping

**What this is:** a map from obligations your compliance team already has to
evidence Aperture produces automatically.

**What this is not:** a certification, an attestation, or legal advice. No tool
makes an organization compliant. Aperture generates a specific, verifiable subset
of the evidence these frameworks ask for, and this page says exactly which subset
so nobody has to guess.

---

## EU AI Act

High-risk AI systems carry record-keeping and human-oversight obligations. The
provisions below are the ones a retrieval-and-action layer can actually serve.

### Article 12 — Record-keeping (logging)

> Automatic recording of events over the lifetime of the system.

| Obligation | Evidence produced |
|---|---|
| Events recorded automatically throughout the lifecycle | Every read and every action, including every refusal, appends a lineage entry. There is no code path that returns data without logging first. |
| Records enable traceability of the system's functioning | Each entry carries the identity, declared purpose, question, sources consulted, records returned, and everything withheld with a reason code. |
| Records appropriate to the intended purpose | Purpose is a first-class field, not an inference. |
| Integrity of records | The log is a hash chain; `aperture lineage verify` names any altered entry. Signed checkpoints detect truncation and wholesale rewriting. |

```bash
aperture lineage verify --secret-env APERTURE_ANCHOR_SECRET
aperture lineage export --since 0 > evidence.ndjson
```

### Article 14 — Human oversight

| Obligation | Evidence produced |
|---|---|
| Humans can intervene or interrupt | Actions above configured thresholds require a named human approval before execution. There is no agent-accessible approve tool. |
| Oversight measures are built into the system | Approval is a state transition in the gateway, not a convention. A pending proposal cannot execute. |
| The human understands the system's capacities and limits | Every approval is presented with a measured blast radius: records affected, monetary impact, external recipients, reversibility. |
| Ability to disregard or reverse output | Reversible actions record a compensating operation; `aperture actions rollback` performs it. Irreversible actions are labelled as such before approval, not after. |
| Avoiding automation bias | Refusals carry machine-readable reason codes and the model is instructed to disclose them, so an incomplete answer is visibly incomplete. |

### Article 10 — Data governance

| Obligation | Evidence produced |
|---|---|
| Relevant, representative data | Sources are registered with an owner and a description of what they answer. |
| Examination for biases | Not addressed. Aperture governs access, not dataset composition. |
| Data minimisation in operation | Purpose binding restricts each access to sources permitted for the declared purpose; field-level redaction removes what is not needed. |

---

## GDPR

| Article | Obligation | Evidence produced |
|---|---|---|
| 5(1)(b) | Purpose limitation | Purpose is declared per request, verified inside the signature, and enforced per source and per action. This is the closest thing to a native implementation of the principle in an agent stack. |
| 5(1)(c) | Data minimisation | Token budgets, record ceilings, and field redaction bound what reaches the model. |
| 5(2) | Accountability | The lineage log demonstrates what was accessed and why. |
| 25 | Data protection by design | Default deny, fail closed, most-restrictive treatment of missing metadata. |
| 30 | Records of processing | `aperture lineage export` produces a per-access record including purpose. |
| 32 | Security of processing | Access control, integrity checks, and audit are enforced in one place rather than per application. |
| 33 | Breach notification | The log answers what was actually accessed during an incident window, which is usually the hardest question to answer. |

---

## NIST AI Risk Management Framework

| Function | Category | How Aperture contributes |
|---|---|---|
| GOVERN | 1.2 Policies are in place | Access policy is a reviewable YAML document under version control, not code |
| GOVERN | 4.1 Risk culture, documented decisions | Every approval records who decided, when, and their note |
| MAP | 2.3 Capabilities and limits documented | The catalog states what each source answers and what each action does, including whether it can be undone |
| MEASURE | 2.7 Security and resilience | Red-team suite runs 26 documented attacks in CI |
| MEASURE | 2.8 Transparency | Refusals carry reason codes; nothing is withheld silently |
| MANAGE | 2.3 Mechanisms to supersede or deactivate | Human approval gate and rollback |
| MANAGE | 4.1 Post-deployment monitoring | Prometheus metrics per purpose, action, and reason code |

---

## ISO/IEC 42001 (AI management systems)

| Clause | Requirement | Evidence produced |
|---|---|---|
| 8.2 | AI system impact assessment | Blast radius is a measured, recorded impact statement per action |
| 8.3 | Operational controls | Policy, budgets, and approvals are configuration, not code changes |
| 9.1 | Monitoring and measurement | Metrics and structured logs |
| 9.2 | Internal audit | Exportable, tamper-evident access records |
| 10.1 | Nonconformity and corrective action | Stranded executions are surfaced for human resolution rather than retried |

---

## SOC 2 (Trust Services Criteria)

| Criterion | How Aperture contributes |
|---|---|
| CC6.1 Logical access | Identity, purpose, ACL, clearance, and tenant enforced at one chokepoint |
| CC6.2 Registration and authorisation | Principals and permissions are declarative and reviewable |
| CC6.3 Role-based access | Group and clearance based, with purpose as an additional dimension |
| CC7.2 Monitoring for anomalies | Refusal counters per reason code make an agent probing its limits visible |
| CC7.3 Incident evaluation | Lineage answers what was accessed, by whom, under what purpose |
| CC8.1 Change management | Policy changes are diffable; the conformance matrix fails CI when access widens |

The **policy conformance matrix** (`tests/test_conformance.py`) deserves specific
mention to an auditor: it asserts, for every principal and purpose pair, exactly
which sources are reachable. It is simultaneously a test, a specification, and a
control that fails the build when access broadens unintentionally.

---

## What Aperture does not give you

- No dataset bias analysis, model evaluation, or model cards.
- No DLP. It governs what reaches the model, not what a person does with an
  answer they were entitled to receive.
- No retention or deletion lifecycle for the sources themselves.
- No attestation. Evidence still has to be reviewed by people who sign things.

## Producing an evidence pack

```bash
# 1. Prove the audit chain is intact and anchored
aperture lineage verify --secret-env APERTURE_ANCHOR_SECRET

# 2. Export access records for the audit window
aperture lineage export --since 0 > access-records.ndjson

# 3. Show the effective access matrix as configuration, not assertion
aperture lint -w workspace
python -m pytest tests/test_conformance.py -v

# 4. Show every action taken, by whom, approved by whom
aperture actions history
```
