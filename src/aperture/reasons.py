"""Machine-readable reason codes.

Every decision Aperture makes carries one of these. Reason codes are part of the
public contract: agents surface them to users, auditors query them in the lineage
log, and the policy conformance suite asserts on them.
"""

from __future__ import annotations

from enum import Enum


class Reason(str, Enum):
    """Why a record or source was allowed, withheld, or altered.

    Subclasses ``str`` rather than ``StrEnum`` so the package runs on Python 3.10,
    which the project supports.
    """

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

    # Action governance (v2)
    ACTION_NOT_REGISTERED = "action_not_registered"
    ACTION_NOT_PERMITTED = "action_not_permitted"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_DENIED = "approval_denied"
    IMPACT_LIMIT_EXCEEDED = "impact_limit_exceeded"
    IRREVERSIBLE_BLOCKED = "irreversible_blocked"
    PROPOSAL_NOT_FOUND = "proposal_not_found"
    PROPOSAL_EXPIRED = "proposal_expired"
    ALREADY_EXECUTED = "already_executed"
    ARGUMENTS_CHANGED = "arguments_changed"
    EXECUTION_FAILED = "execution_failed"
    ROLLBACK_UNSUPPORTED = "rollback_unsupported"
    INVALID_ARGUMENTS = "invalid_arguments"
    SELF_APPROVAL_FORBIDDEN = "self_approval_forbidden"
    APPROVER_NOT_AUTHORIZED = "approver_not_authorized"
    PROPOSAL_IN_FLIGHT = "proposal_in_flight"
    SPEND_LIMIT_EXCEEDED = "spend_limit_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    INVALID_ASSERTION = "invalid_assertion"
    ASSERTION_EXPIRED = "assertion_expired"
    ASSERTION_REPLAYED = "assertion_replayed"

    def __str__(self) -> str:
        return self.value


HUMAN_READABLE: dict[Reason, str] = {
    Reason.ALLOWED: "allowed by policy",
    Reason.NO_MATCHING_RULE: "no policy rule grants access (default deny)",
    Reason.EXPLICIT_DENY: "an explicit deny rule matched",
    Reason.PURPOSE_NOT_PERMITTED: "the declared purpose is not permitted here",
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
    Reason.ACTION_NOT_REGISTERED: "action is not registered in the action catalog",
    Reason.ACTION_NOT_PERMITTED: "no policy rule permits this principal to take this action",
    Reason.APPROVAL_REQUIRED: "a human must approve this action before it can run",
    Reason.APPROVAL_MISSING: "execution attempted without an approval decision",
    Reason.APPROVAL_DENIED: "a human reviewer rejected this action",
    Reason.IMPACT_LIMIT_EXCEEDED: "estimated impact exceeds the limit set by policy",
    Reason.IRREVERSIBLE_BLOCKED: "action cannot be undone and policy forbids it here",
    Reason.PROPOSAL_NOT_FOUND: "no such proposal",
    Reason.PROPOSAL_EXPIRED: "proposal is older than the execution window",
    Reason.ALREADY_EXECUTED: "proposal has already been executed",
    Reason.ARGUMENTS_CHANGED: "arguments differ from the approved proposal",
    Reason.EXECUTION_FAILED: "the action failed while running",
    Reason.ROLLBACK_UNSUPPORTED: "this action declares no compensating operation",
    Reason.INVALID_ARGUMENTS: "arguments do not match the action's declared parameters",
    Reason.SELF_APPROVAL_FORBIDDEN: "the proposer may not approve their own action",
    Reason.APPROVER_NOT_AUTHORIZED: "this identity may not approve this action",
    Reason.PROPOSAL_IN_FLIGHT: "another caller is already executing this proposal",
    Reason.SPEND_LIMIT_EXCEEDED: "this would exceed the spend budget for the current window",
    Reason.RATE_LIMIT_EXCEEDED: "too many actions of this kind in the current window",
    Reason.INVALID_ASSERTION: "caller assertion is missing, malformed, or badly signed",
    Reason.ASSERTION_EXPIRED: "caller assertion is outside its validity window",
    Reason.ASSERTION_REPLAYED: "caller assertion has already been used",
}


def explain(reason: Reason) -> str:
    """Return the human-readable form of a reason code."""
    return HUMAN_READABLE.get(reason, str(reason))
