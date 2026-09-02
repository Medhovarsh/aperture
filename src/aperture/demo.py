"""Demo workspace generator.

Builds a small but realistic multi-tenant enterprise: HR policies, engineering
runbooks, a support knowledge base, and an employee database. The fixture is shaped
to exercise every gate in the pipeline, so `aperture demo` doubles as the acceptance
test for the product claim:

* purpose binding - the same person asking the same question gets different data
  under a different declared purpose
* clearance - a restricted document is withheld with a reason, not silently dropped
* field redaction - salary and national ID are scrubbed while the row stays useful
* tenant isolation - a partner in another tenant sees nothing of Acme's
* freshness - a document past its SLA is returned tagged, or dropped, per policy
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATALOG = """\
# Registered sources. A source that is not listed here does not exist to agents.
sources:
  - id: hr_handbook
    kind: docs
    title: HR Handbook
    description: >-
      Employee handbook and people policies: parental leave, paid time off,
      expenses, remote work, performance review cycles, termination process.
    owner: people-ops@acme.example
    sensitivity: confidential
    freshness_sla_days: 400
    allowed_purposes: [hr_support, security_audit]
    tags: [hr, policy, benefits]
    config:
      path: data/hr

  - id: eng_runbooks
    kind: docs
    title: Engineering Runbooks
    description: >-
      Production runbooks and on-call procedures: incident severity levels,
      database failover, deploy rollback, paging escalation, service ownership.
    owner: platform@acme.example
    sensitivity: internal
    freshness_sla_days: 180
    allowed_purposes: [engineering_oncall, security_audit]
    tags: [engineering, oncall, incident]
    config:
      path: data/eng

  - id: support_kb
    kind: vector
    title: Customer Support Knowledge Base
    description: >-
      Customer-facing answers about billing, refunds, plan limits, password
      resets, and outage communication templates.
    owner: support@acme.example
    sensitivity: internal
    freshness_sla_days: 90
    allowed_purposes: [customer_support, security_audit]
    tags: [support, billing, customers]
    config:
      path: data/support/kb.jsonl

  - id: people_db
    kind: sql
    title: Employee Directory
    description: >-
      Employee records: name, role, manager, office location, start date,
      compensation, and national identifiers.
    owner: people-ops@acme.example
    sensitivity: restricted
    freshness_sla_days: null
    allowed_purposes: [hr_support, security_audit]
    tags: [hr, people, pii]
    config:
      database: data/people.db
      table: employees
      id_column: id
      text_columns: [name, role, manager, location]
      acl_column: acl
      tenant_column: tenant
      updated_column: updated_at
"""

POLICY = """\
version: 1

# Purposes an agent may declare. An unregistered purpose is denied outright.
purposes:
  - hr_support
  - engineering_oncall
  - customer_support
  - security_audit

defaults:
  # "tag" returns stale records flagged; "drop" withholds them with reason=stale.
  stale_action: tag

rules:
  # --- baseline: employees may read internal material ---------------------
  - id: employees-read-internal
    effect: allow
    description: Any Acme employee may read internal sources.
    when:
      groups: [employees]
      tenants: [acme]
      sensitivity_at_most: internal

  # --- HR ------------------------------------------------------------------
  - id: hr-reads-handbook
    effect: allow
    description: HR may read the handbook when acting for HR support.
    when:
      groups: [hr]
      purposes: [hr_support]
      sources: [hr_handbook]

  - id: hr-reads-directory
    effect: allow
    description: HR may read the employee directory for HR support only.
    when:
      groups: [hr]
      purposes: [hr_support]
      sources: [people_db]

  - id: redact-compensation
    effect: redact
    description: Compensation and national ID are hidden from everyone outside HR comp.
    when:
      sources: [people_db]
    redact_fields: [salary, national_id]

  # --- auditors ------------------------------------------------------------
  - id: auditor-read-all
    effect: allow
    description: Security auditors may read every source under a security audit.
    when:
      groups: [security-audit]
      purposes: [security_audit]

  # --- hard denials --------------------------------------------------------
  - id: deny-partners-confidential
    effect: deny
    description: Partners never see confidential or restricted material.
    when:
      groups: [partners]
      sensitivity_at_least: confidential
"""

PRINCIPALS = """\
principals:
  - id: u_dana
    display_name: Dana Whitfield (People Ops)
    tenant: acme
    groups: [employees, hr]
    clearance: restricted

  - id: u_raj
    display_name: Raj Mehta (Platform Engineer)
    tenant: acme
    groups: [employees, engineering]
    clearance: internal

  - id: u_kim
    display_name: Kim Alvarez (Support Lead)
    tenant: acme
    groups: [employees, support]
    clearance: internal

  - id: svc_support_agent
    display_name: Support Copilot (service account)
    tenant: acme
    groups: [employees, support]
    clearance: internal

  - id: u_sam
    display_name: Sam Oyelaran (Security Audit)
    tenant: acme
    groups: [employees, security-audit]
    clearance: restricted

  - id: u_partner
    display_name: Priya Nandakumar (Globex Partner)
    tenant: globex
    groups: [partners]
    clearance: public
"""


def _doc(
    title: str,
    acl: list[str],
    sensitivity: str,
    updated: datetime,
    body: str,
    tags: list[str],
) -> str:
    """Render a Markdown document with governance frontmatter."""
    frontmatter = {
        "title": title,
        "acl": acl,
        "tenant": "acme",
        "sensitivity": sensitivity,
        "updated_at": updated.date().isoformat(),
        "tags": tags,
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(textwrap.dedent(body).strip())
    lines.append("")
    return "\n".join(lines)


def build_demo_workspace(root: Path) -> Path:
    """Create a complete demo workspace at ``root`` and return the path."""
    root = Path(root)
    (root / "data" / "hr").mkdir(parents=True, exist_ok=True)
    (root / "data" / "eng").mkdir(parents=True, exist_ok=True)
    (root / "data" / "support").mkdir(parents=True, exist_ok=True)
    (root / "lineage").mkdir(parents=True, exist_ok=True)

    (root / "catalog.yaml").write_text(CATALOG, encoding="utf-8")
    (root / "policy.yaml").write_text(POLICY, encoding="utf-8")
    (root / "principals.yaml").write_text(PRINCIPALS, encoding="utf-8")

    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=30)
    old = now - timedelta(days=500)

    hr = root / "data" / "hr"
    hr.joinpath("parental-leave.md").write_text(
        _doc(
            "Parental Leave Policy",
            ["hr", "people-managers"],
            "confidential",
            recent,
            """
            Birthing parents receive 18 weeks of fully paid leave. Non-birthing
            parents receive 12 weeks, which may be taken in up to three separate
            blocks during the first year. Leave requests go to People Ops at least
            30 days before the intended start date where practical.

            Leave accrues no vacation time. Health coverage continues unchanged
            for the full duration.
            """,
            ["policy", "benefits", "leave"],
        ),
        encoding="utf-8",
    )
    hr.joinpath("expenses.md").write_text(
        _doc(
            "Expense Reimbursement",
            ["employees"],
            "internal",
            recent,
            """
            Submit expenses within 60 days. Meals while travelling are capped at
            75 USD per day. Any single item above 500 USD needs manager approval
            before purchase. Reimbursement lands in the next payroll run after
            approval.
            """,
            ["policy", "expenses"],
        ),
        encoding="utf-8",
    )
    hr.joinpath("termination.md").write_text(
        _doc(
            "Involuntary Termination Procedure",
            ["hr"],
            "restricted",
            old,
            """
            Terminations require a documented performance record, a review by
            People Ops, and legal sign-off in regulated jurisdictions. Severance
            follows the tenure schedule in Appendix C. Access revocation is
            triggered by People Ops at the start of the notification meeting.
            """,
            ["policy", "termination"],
        ),
        encoding="utf-8",
    )

    eng = root / "data" / "eng"
    eng.joinpath("database-failover.md").write_text(
        _doc(
            "Database Failover Runbook",
            ["engineering", "employees"],
            "internal",
            recent,
            """
            Promote the standby replica with `pg_ctl promote` only after confirming
            replication lag is under 5 seconds. Update the connection string in the
            service mesh config, then drain the old primary. Expect roughly 40
            seconds of write unavailability during promotion.
            """,
            ["oncall", "database", "incident"],
        ),
        encoding="utf-8",
    )
    eng.joinpath("incident-severity.md").write_text(
        _doc(
            "Incident Severity Levels",
            ["employees"],
            "internal",
            recent,
            """
            SEV1 is a full customer-facing outage and pages the on-call lead plus
            the incident commander rotation. SEV2 is degraded performance for a
            subset of customers. SEV3 is an internal-only issue with a workaround.
            Only SEV1 and SEV2 require a public status page update.
            """,
            ["oncall", "incident", "process"],
        ),
        encoding="utf-8",
    )
    eng.joinpath("deploy-rollback.md").write_text(
        _doc(
            "Deploy Rollback",
            ["engineering"],
            "internal",
            old,
            """
            Roll back with the previous release tag. Database migrations are not
            reversed automatically; check the migration ledger before rolling back
            past a schema change.
            """,
            ["oncall", "deploy"],
        ),
        encoding="utf-8",
    )

    kb_chunks = [
        {
            "id": "kb-refund-window",
            "title": "Refund window",
            "text": (
                "Customers may request a full refund within 30 days of the charge. "
                "After 30 days, offer account credit instead. Annual plans are "
                "refunded pro rata."
            ),
            "acl": ["support", "employees"],
            "tenant": "acme",
            "sensitivity": "internal",
            "updated_at": recent.isoformat(),
        },
        {
            "id": "kb-password-reset",
            "title": "Password reset",
            "text": (
                "Send the self-service reset link. Never read a reset code aloud "
                "or accept one over chat. If the customer cannot access their email, "
                "escalate to identity support rather than changing the address."
            ),
            "acl": ["support", "employees"],
            "tenant": "acme",
            "sensitivity": "internal",
            "updated_at": recent.isoformat(),
        },
        {
            "id": "kb-plan-limits",
            "title": "Plan limits",
            "text": (
                "Team plan includes 25 seats and 500 GB of storage. Overage is "
                "billed monthly at 0.10 USD per GB. Enterprise limits are contractual."
            ),
            "acl": ["support", "employees"],
            "tenant": "acme",
            "sensitivity": "internal",
            "updated_at": old.isoformat(),
        },
        {
            "id": "kb-globex-onboarding",
            "title": "Globex partner onboarding",
            "text": "Globex partners provision seats through their own tenant admin console.",
            "acl": ["partners"],
            "tenant": "globex",
            "sensitivity": "internal",
            "updated_at": recent.isoformat(),
        },
    ]
    (root / "data" / "support" / "kb.jsonl").write_text(
        "\n".join(json.dumps(chunk) for chunk in kb_chunks) + "\n", encoding="utf-8"
    )

    db_path = root / "data" / "people.db"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE employees (
            id TEXT PRIMARY KEY,
            name TEXT,
            role TEXT,
            manager TEXT,
            location TEXT,
            salary TEXT,
            national_id TEXT,
            tenant TEXT,
            acl TEXT,
            updated_at TEXT
        )
        """
    )
    rows = [
        ("e-1001", "Dana Whitfield", "People Ops Lead", "Ana Duarte", "Austin",
         "184000", "532-88-4410", "acme", "hr,employees", recent.isoformat()),
        ("e-1002", "Raj Mehta", "Staff Platform Engineer", "Ana Duarte", "Bengaluru",
         "212000", "641-22-9087", "acme", "hr,employees", recent.isoformat()),
        ("e-1003", "Kim Alvarez", "Support Lead", "Dana Whitfield", "Lisbon",
         "141000", "778-31-2245", "acme", "hr,employees", recent.isoformat()),
        ("e-2001", "Priya Nandakumar", "Partner Architect", "Globex Mgmt", "Mumbai",
         "0", "000-00-0000", "globex", "partners", recent.isoformat()),
    ]
    connection.executemany(
        "INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?)", rows
    )
    connection.commit()
    connection.close()

    return root
