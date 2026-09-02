"""Machine-readable reason codes.

Every decision Aperture makes carries one of these. Reason codes are part of the
public contract: agents surface them to users, auditors query them in the lineage
log, and the policy conformance suite asserts on them.
"""

from __future__ import annotations

from enum import StrEnum


class Reason(StrEnum):
    """Why a record or source was allowed, withheld, or altered."""

    # Allow
    ALLOWED = "allowed"

    # Policy outcomes
    NO_MATCHING_RULE = "no_matching_rule"
    EXPLICIT_DENY = "explicit_deny"
    PURPOSE_NOT_PERMITTED = "purpose_not_permitted"
    SOURCE_NOT_ELIGIBLE = "source_not_eligible"
    POLICY_ERROR = "policy_error"

    # Identity
    UNKNOWN_PRINCIPAL = "unknown_principal"

    # Record-level enforcement
    ACL_MISMATCH = "acl_mismatch"
    MISSING_ACL = "missing_acl"
    TENANT_MISMATCH = "tenant_mismatch"
    INSUFFICIENT_CLEARANCE = "insufficient_clearance"

    # Data quality gates
    STALE = "stale"
    MISSING_TIMESTAMP = "missing_timestamp"

    # Shaping
    REDACTED = "redacted"
    BUDGET_TRUNCATED = "budget_truncated"

    # Infrastructure
    SOURCE_UNAVAILABLE = "source_unavailable"


HUMAN_READABLE: dict[Reason, str] = {
    Reason.ALLOWED: "allowed by policy",
    Reason.NO_MATCHING_RULE: "no policy rule grants access (default deny)",
    Reason.EXPLICIT_DENY: "an explicit deny rule matched",
    Reason.PURPOSE_NOT_PERMITTED: "the declared purpose is not permitted for this source",
    Reason.SOURCE_NOT_ELIGIBLE: "principal may not read this source under this purpose",
    Reason.POLICY_ERROR: "policy evaluation failed; denied fail-closed",
    Reason.UNKNOWN_PRINCIPAL: "caller identity is not registered",
    Reason.ACL_MISMATCH: "record ACL does not include the principal",
    Reason.MISSING_ACL: "record has no ACL metadata; treated as most restrictive",
    Reason.TENANT_MISMATCH: "record belongs to a different tenant",
    Reason.INSUFFICIENT_CLEARANCE: "record sensitivity exceeds principal clearance",
    Reason.STALE: "record is older than the source freshness SLA",
    Reason.MISSING_TIMESTAMP: "record has no timestamp and the source requires freshness",
    Reason.REDACTED: "one or more fields were redacted by policy",
    Reason.BUDGET_TRUNCATED: "dropped to fit the response token budget",
    Reason.SOURCE_UNAVAILABLE: "source could not be queried",
}


def explain(reason: Reason) -> str:
    """Return the human-readable form of a reason code."""
    return HUMAN_READABLE.get(reason, str(reason))
