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

ACTIONS = """\
# Registered actions. An action absent from this file cannot be proposed.
# 'reversible' is verified against the executor at load time: an action cannot
# claim to be undoable unless its executor implements a compensating operation.
actions:
  - id: support.refund
    title: Issue a refund
    description: Refund a customer, in USD, against their account.
    executor: refund
    owner: support@acme.example
    effect_class: financial
    reversible: true
    allowed_purposes: [customer_support]
    parameters:
      customer_id: {type: string, description: Customer to refund}
      amount: {type: number, description: Amount in USD}
    config:
      database: data/ops.db
      approver_groups: [support-leads]

  - id: support.close_ticket
    title: Close a support ticket
    description: Mark a support ticket resolved.
    executor: ticket
    owner: support@acme.example
    effect_class: write
    reversible: true
    allowed_purposes: [customer_support]
    parameters:
      ticket_id: {type: string, description: Ticket to close}
    config:
      database: data/ops.db

  - id: support.message_customer
    title: Email a customer
    description: Send a message to an address outside the company.
    executor: message
    owner: support@acme.example
    effect_class: external
    reversible: false
    allowed_purposes: [customer_support]
    parameters:
      to: {type: string, description: Recipient address}
      subject: {type: string, description: Subject line}
      body: {type: string, description: Message body}
    config:
      database: data/ops.db
      approver_groups: [support-leads]

  - id: ops.purge_region
    title: Purge a region
    description: Permanently delete every customer account in a region.
    executor: account_purge
    owner: platform@acme.example
    effect_class: destructive
    reversible: false
    allowed_purposes: [data_retention]
    parameters:
      region: {type: string, description: Region code to purge}
    config:
      database: data/ops.db
      approver_groups: [security-audit]
"""

POLICY = """\
version: 1

# Purposes an agent may declare. An unregistered purpose is denied outright.
purposes:
  - hr_support
  - engineering_oncall
  - customer_support
  - security_audit
  - data_retention

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

  # --- actions (v2) --------------------------------------------------------
  # These rules name actions, so they govern actions only. No amount of read
  # access can add up to permission to act.

  - id: support-small-refunds
    effect: allow
    description: Support may refund up to 100 USD without asking anyone.
    when:
      groups: [support]
      purposes: [customer_support]
      actions: [support.refund]
    max_amount: 100

  - id: support-large-refunds
    effect: allow
    description: Larger refunds are allowed up to 5000 USD, with a human approval.
    when:
      groups: [support]
      purposes: [customer_support]
      actions: [support.refund]
    max_amount: 5000
    requires_approval: true

  - id: support-close-tickets
    effect: allow
    description: Closing a ticket is reversible and needs no approval.
    when:
      groups: [support]
      purposes: [customer_support]
      actions: [support.close_ticket]

  - id: support-external-messages
    effect: allow
    description: Anything that leaves the company needs a human first.
    when:
      groups: [support]
      purposes: [customer_support]
      actions: [support.message_customer]
    requires_approval: true

  - id: platform-region-purge
    effect: allow
    description: Platform may purge a region, with approval, up to 5 accounts.
    when:
      groups: [platform]
      purposes: [data_retention]
      actions: [ops.purge_region]
    max_affected: 5
    requires_approval: true

  - id: deny-partners-actions
    effect: deny
    description: Partners may not take any action in this tenant.
    when:
      groups: [partners]
      actions: ["*"]

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
    groups: [employees, support, support-leads]
    clearance: internal

  - id: svc_support_agent
    display_name: Support Copilot (service account)
    tenant: acme
    groups: [employees, support]
    clearance: internal

  - id: u_ops
    display_name: Ana Duarte (Platform Lead)
    tenant: acme
    groups: [employees, platform, platform-leads]
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
    (root / "actions.yaml").write_text(ACTIONS, encoding="utf-8")

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

    _build_operations_db(root / "data" / "ops.db")

    return root


def _build_operations_db(db_path: Path) -> None:
    """Create the systems actions operate on: customers, tickets, refunds, messages."""
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY,
            name TEXT,
            region TEXT,
            lifetime_value REAL
        );
        CREATE TABLE tickets (
            ticket_id TEXT PRIMARY KEY,
            customer_id TEXT,
            subject TEXT,
            status TEXT
        );
        CREATE TABLE refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT,
            amount REAL,
            status TEXT,
            created_at TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT,
            subject TEXT,
            body TEXT,
            sent_at TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO customers VALUES (?,?,?,?)",
        [
            ("cus-4471", "Rivera Logistics", "us-east", 42000.0),
            ("cus-5510", "Northwind Books", "us-east", 8800.0),
            ("cus-6602", "Kestrel Design", "eu-west", 15600.0),
            ("cus-7713", "Marabou Foods", "eu-west", 3100.0),
            ("cus-8890", "Lumen Analytics", "eu-west", 26400.0),
            ("cus-9021", "Tessellate Labs", "apac", 5200.0),
            # A region large enough that purging it breaches the policy limit,
            # so the demo can show impact_limit_exceeded rather than only describe it.
            ("cus-1001", "Halcyon Print", "legacy", 900.0),
            ("cus-1002", "Orbit Stationers", "legacy", 1200.0),
            ("cus-1003", "Pallas Textiles", "legacy", 640.0),
            ("cus-1004", "Vesper Cycles", "legacy", 2300.0),
            ("cus-1005", "Wren Ceramics", "legacy", 480.0),
            ("cus-1006", "Yarrow Optics", "legacy", 1750.0),
            ("cus-1007", "Zephyr Tooling", "legacy", 3050.0),
        ],
    )
    connection.executemany(
        "INSERT INTO tickets VALUES (?,?,?,?)",
        [
            ("tkt-1180", "cus-4471", "Duplicate charge on August invoice", "open"),
            ("tkt-1181", "cus-5510", "Cannot reset password", "open"),
            ("tkt-1182", "cus-6602", "Requesting plan downgrade", "pending"),
        ],
    )
    connection.commit()
    connection.close()
