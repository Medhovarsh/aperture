"""Record-level gate behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aperture.enforcement import REDACTION_MARKER, Enforcer
from aperture.policy import Policy
from aperture.reasons import Reason
from aperture.types import Principal, Record, Sensitivity, Source

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)

PRINCIPAL = Principal(
    id="u1", tenant="acme", groups=("employees", "support"), clearance=Sensitivity.INTERNAL
)

SOURCE = Source(
    id="kb",
    kind="docs",
    title="KB",
    description="knowledge base",
    owner="o",
    sensitivity=Sensitivity.INTERNAL,
)

ALLOW_ALL = Policy.from_dict({"version": 1, "rules": [{"id": "a", "effect": "allow", "when": {}}]})


def record(**overrides) -> Record:
    base = {
        "id": "r1",
        "source_id": "kb",
        "title": "Doc",
        "text": "body text",
        "tenant": "acme",
        "acl": ("employees",),
        "updated_at": NOW - timedelta(days=1),
        "score": 1.0,
    }
    base.update(overrides)
    return Record(**base)


def run(enforcer: Enforcer, rec: Record, source: Source = SOURCE, **kwargs):
    defaults = {"max_records": 10, "token_budget": 10_000, "now": NOW}
    defaults.update(kwargs)
    return enforcer.apply(PRINCIPAL, "any", [(source, rec)], **defaults)


def reasons(outcome) -> set[Reason]:
    return {group.reason for group in outcome.withheld}


def test_allows_a_permitted_record() -> None:
    outcome = run(Enforcer(ALLOW_ALL), record())
    assert [r.id for r in outcome.kept] == ["r1"]
    assert outcome.withheld == []


def test_cross_tenant_record_is_withheld() -> None:
    outcome = run(Enforcer(ALLOW_ALL), record(tenant="globex"))
    assert outcome.kept == []
    assert reasons(outcome) == {Reason.TENANT_MISMATCH}


def test_acl_mismatch_is_withheld() -> None:
    outcome = run(Enforcer(ALLOW_ALL), record(acl=("finance",)))
    assert reasons(outcome) == {Reason.ACL_MISMATCH}


def test_wildcard_acl_is_honored() -> None:
    outcome = run(Enforcer(ALLOW_ALL), record(acl=("*",)))
    assert len(outcome.kept) == 1


def test_missing_acl_is_treated_as_restrictive() -> None:
    """Absent metadata must fail closed, not open."""
    outcome = run(Enforcer(ALLOW_ALL), record(acl=None))
    assert outcome.kept == []
    assert reasons(outcome) == {Reason.MISSING_ACL}


def test_missing_acl_uses_catalog_default_when_declared() -> None:
    source = SOURCE.model_copy(update={"config": {"default_acl": ["employees"]}})
    outcome = run(Enforcer(ALLOW_ALL), record(acl=None), source=source)
    assert len(outcome.kept) == 1


def test_clearance_ceiling_is_enforced() -> None:
    outcome = run(Enforcer(ALLOW_ALL), record(sensitivity=Sensitivity.RESTRICTED))
    assert reasons(outcome) == {Reason.INSUFFICIENT_CLEARANCE}


def test_record_sensitivity_overrides_source_sensitivity() -> None:
    restricted_source = SOURCE.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
    outcome = run(
        Enforcer(ALLOW_ALL), record(sensitivity=Sensitivity.PUBLIC), source=restricted_source
    )
    assert len(outcome.kept) == 1


def test_stale_record_is_tagged_by_default() -> None:
    source = SOURCE.model_copy(update={"freshness_sla_days": 30})
    outcome = run(Enforcer(ALLOW_ALL), record(updated_at=NOW - timedelta(days=200)), source=source)
    assert len(outcome.kept) == 1
    assert any("older than" in note for note in outcome.kept[0].notes)


def test_stale_record_is_dropped_when_policy_says_drop() -> None:
    policy = Policy.from_dict(
        {
            "version": 1,
            "defaults": {"stale_action": "drop"},
            "rules": [{"id": "a", "effect": "allow", "when": {}}],
        }
    )
    source = SOURCE.model_copy(update={"freshness_sla_days": 30})
    outcome = run(Enforcer(policy), record(updated_at=NOW - timedelta(days=200)), source=source)
    assert outcome.kept == []
    assert reasons(outcome) == {Reason.STALE}


def test_missing_timestamp_flagged_when_source_has_an_sla() -> None:
    source = SOURCE.model_copy(update={"freshness_sla_days": 30})
    outcome = run(Enforcer(ALLOW_ALL), record(updated_at=None), source=source)
    assert len(outcome.kept) == 1
    assert any("no timestamp" in note for note in outcome.kept[0].notes)


def test_redaction_removes_field_and_scrubs_it_from_text() -> None:
    """A redacted value that also appears in free text is still a leak."""
    policy = Policy.from_dict(
        {
            "version": 1,
            "rules": [
                {"id": "a", "effect": "allow", "when": {}},
                {"id": "r", "effect": "redact", "when": {}, "redact_fields": ["salary"]},
            ],
        }
    )
    rec = record(
        text="Kim earns 141000 per year and reports to Dana.",
        fields={"salary": "141000", "manager": "Dana"},
    )
    outcome = run(Enforcer(policy), rec)
    kept = outcome.kept[0]
    assert "141000" not in kept.text
    assert REDACTION_MARKER in kept.text
    assert kept.redacted_fields == ("salary",)


def test_budget_truncation_is_reported() -> None:
    enforcer = Enforcer(ALLOW_ALL)
    candidates = [
        (SOURCE, record(id=f"r{i}", text="x" * 400, score=float(10 - i))) for i in range(5)
    ]
    outcome = enforcer.apply(
        PRINCIPAL, "any", candidates, max_records=2, token_budget=10_000, now=NOW
    )
    assert len(outcome.kept) == 2
    assert Reason.BUDGET_TRUNCATED in reasons(outcome)


def test_highest_scoring_records_survive_truncation() -> None:
    enforcer = Enforcer(ALLOW_ALL)
    candidates = [(SOURCE, record(id=f"r{i}", score=float(i))) for i in range(4)]
    outcome = enforcer.apply(
        PRINCIPAL, "any", candidates, max_records=2, token_budget=10_000, now=NOW
    )
    assert [r.id for r in outcome.kept] == ["r3", "r2"]


@pytest.mark.parametrize(
    ("clearance", "sensitivity", "expected"),
    [
        (Sensitivity.PUBLIC, Sensitivity.PUBLIC, True),
        (Sensitivity.PUBLIC, Sensitivity.INTERNAL, False),
        (Sensitivity.INTERNAL, Sensitivity.CONFIDENTIAL, False),
        (Sensitivity.CONFIDENTIAL, Sensitivity.CONFIDENTIAL, True),
        (Sensitivity.RESTRICTED, Sensitivity.CONFIDENTIAL, True),
    ],
)
def test_clearance_matrix(clearance: Sensitivity, sensitivity: Sensitivity, expected: bool) -> None:
    principal = PRINCIPAL.model_copy(update={"clearance": clearance})
    outcome = Enforcer(ALLOW_ALL).apply(
        principal,
        "any",
        [(SOURCE, record(sensitivity=sensitivity))],
        max_records=5,
        token_budget=10_000,
        now=NOW,
    )
    assert bool(outcome.kept) is expected
