"""Policy engine semantics."""

from __future__ import annotations

import pytest

from aperture.policy import Policy
from aperture.reasons import Reason
from aperture.types import Decision, Principal, Sensitivity, Source

EMPLOYEE = Principal(id="u1", tenant="acme", groups=("employees",), clearance=Sensitivity.INTERNAL)
CONTRACTOR = Principal(id="u2", tenant="acme", groups=("contractors",), clearance=Sensitivity.PUBLIC)

INTERNAL_SOURCE = Source(
    id="wiki", kind="docs", title="Wiki", description="notes", owner="o", sensitivity=Sensitivity.INTERNAL
)
SECRET_SOURCE = Source(
    id="vault", kind="docs", title="Vault", description="secrets", owner="o", sensitivity=Sensitivity.RESTRICTED
)


def build(rules: list[dict], **kwargs) -> Policy:
    return Policy.from_dict({"version": 1, "rules": rules, **kwargs})


def test_default_deny_with_no_rules() -> None:
    verdict = build([]).evaluate(EMPLOYEE, "any", INTERNAL_SOURCE)
    assert verdict.decision is Decision.DENY
    assert verdict.reason is Reason.NO_MATCHING_RULE


def test_allow_when_rule_matches() -> None:
    policy = build([{"id": "r1", "effect": "allow", "when": {"groups": ["employees"]}}])
    verdict = policy.evaluate(EMPLOYEE, "any", INTERNAL_SOURCE)
    assert verdict.decision is Decision.ALLOW
    assert verdict.matched_rules == ("r1",)


def test_deny_overrides_allow() -> None:
    policy = build(
        [
            {"id": "allow-all", "effect": "allow", "when": {"groups": ["employees"]}},
            {"id": "block-secret", "effect": "deny", "when": {"sources": ["vault"]}},
        ]
    )
    assert policy.evaluate(EMPLOYEE, "any", INTERNAL_SOURCE).decision is Decision.ALLOW
    denied = policy.evaluate(EMPLOYEE, "any", SECRET_SOURCE)
    assert denied.decision is Decision.DENY
    assert denied.reason is Reason.EXPLICIT_DENY


def test_unregistered_purpose_is_denied() -> None:
    policy = build(
        [{"id": "r1", "effect": "allow", "when": {}}],
        purposes=["support"],
    )
    assert policy.evaluate(EMPLOYEE, "support", INTERNAL_SOURCE).decision is Decision.ALLOW
    verdict = policy.evaluate(EMPLOYEE, "exfiltration", INTERNAL_SOURCE)
    assert verdict.decision is Decision.DENY
    assert verdict.reason is Reason.PURPOSE_NOT_PERMITTED


def test_source_purpose_allowlist_is_enforced() -> None:
    scoped = SECRET_SOURCE.model_copy(update={"allowed_purposes": ("audit",)})
    policy = build([{"id": "r1", "effect": "allow", "when": {}}])
    assert policy.evaluate(EMPLOYEE, "support", scoped).reason is Reason.PURPOSE_NOT_PERMITTED
    assert policy.evaluate(EMPLOYEE, "audit", scoped).decision is Decision.ALLOW


def test_sensitivity_bounds() -> None:
    policy = build(
        [{"id": "r1", "effect": "allow", "when": {"sensitivity_at_most": "internal"}}]
    )
    assert policy.evaluate(EMPLOYEE, "any", INTERNAL_SOURCE).decision is Decision.ALLOW
    assert policy.evaluate(EMPLOYEE, "any", SECRET_SOURCE).decision is Decision.DENY


def test_redaction_accumulates_across_rules() -> None:
    policy = build(
        [
            {"id": "allow", "effect": "allow", "when": {}},
            {"id": "r-a", "effect": "redact", "when": {}, "redact_fields": ["ssn"]},
            {"id": "r-b", "effect": "redact", "when": {}, "redact_fields": ["salary", "ssn"]},
        ]
    )
    verdict = policy.evaluate(EMPLOYEE, "any", INTERNAL_SOURCE)
    assert verdict.decision is Decision.REDACT
    assert verdict.redact_fields == ("salary", "ssn")
    assert verdict.permitted


def test_redact_alone_does_not_grant_access() -> None:
    """A redact rule shapes an allow; it must never create one."""
    policy = build([{"id": "r", "effect": "redact", "when": {}, "redact_fields": ["ssn"]}])
    assert policy.evaluate(EMPLOYEE, "any", INTERNAL_SOURCE).decision is Decision.DENY


def test_all_match_dimensions_must_hold() -> None:
    policy = build(
        [
            {
                "id": "narrow",
                "effect": "allow",
                "when": {"groups": ["employees"], "tenants": ["acme"], "purposes": ["support"]},
            }
        ]
    )
    assert policy.evaluate(EMPLOYEE, "support", INTERNAL_SOURCE).decision is Decision.ALLOW
    assert policy.evaluate(EMPLOYEE, "audit", INTERNAL_SOURCE).decision is Decision.DENY
    assert policy.evaluate(CONTRACTOR, "support", INTERNAL_SOURCE).decision is Decision.DENY


def test_duplicate_rule_ids_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate rule id"):
        build([{"id": "x", "effect": "allow"}, {"id": "x", "effect": "deny"}])


def test_evaluation_failure_denies_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash inside evaluation must produce a deny, never an exception or an allow."""
    policy = build([{"id": "r", "effect": "allow", "when": {}}])

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(Policy, "_matches", staticmethod(explode))
    verdict = policy.evaluate(EMPLOYEE, "any", INTERNAL_SOURCE)
    assert verdict.decision is Decision.DENY
    assert verdict.reason is Reason.POLICY_ERROR
