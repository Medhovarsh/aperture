"""Record-level enforcement.

This is where the plane earns its name. Every candidate record passes through a
fixed sequence of gates, and every record that does not survive produces a reason
code instead of quietly disappearing.

Gate order is deliberate - identity before content, content before quality:

1. **Tenant** - cross-tenant records are dropped first and unconditionally.
2. **ACL** - a record with no ACL metadata is treated as most restrictive.
3. **Clearance** - record sensitivity may not exceed the principal's clearance.
4. **Policy** - the declarative rules get the final say and may add redactions.
5. **Freshness** - records past the source SLA are dropped or tagged.
6. **Redaction** - configured fields are removed from structured data and scrubbed
   from text.
7. **Budget** - survivors are truncated to fit the response token budget.

Redaction runs after policy because policy decides which fields to redact, and
before budgeting because redaction changes how many tokens a record costs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, Field

from .policy import Policy
from .reasons import Reason, explain
from .text import estimate_tokens
from .types import (
    Citation,
    Decision,
    Principal,
    Record,
    ResultRecord,
    Sensitivity,
    Source,
    WithheldGroup,
    sensitivity_rank,
)

REDACTION_MARKER = "[REDACTED]"


class EnforcementOutcome(BaseModel):
    """Records that survived, and an accounting of everything that did not."""

    kept: list[ResultRecord] = Field(default_factory=list)
    withheld: list[WithheldGroup] = Field(default_factory=list)


class Enforcer:
    """Applies the gate sequence to candidate records."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def apply(
        self,
        principal: Principal,
        purpose: str,
        candidates: list[tuple[Source, Record]],
        max_records: int,
        token_budget: int,
        now: datetime | None = None,
    ) -> EnforcementOutcome:
        """Filter, redact, and budget candidate records."""
        withheld: dict[Reason, set[str]] = defaultdict(set)
        withheld_counts: dict[Reason, int] = defaultdict(int)

        def withhold(reason: Reason, source_id: str) -> None:
            withheld_counts[reason] += 1
            withheld[reason].add(source_id)

        survivors: list[ResultRecord] = []

        for source, record in candidates:
            reason = self._screen(principal, purpose, source, record, now)
            if reason is not None:
                withhold(reason, source.id)
                continue

            verdict = self.policy.evaluate(principal, purpose, source, record)
            if not verdict.permitted:
                withhold(verdict.reason, source.id)
                continue

            stale_reason = self._freshness(source, record, now)
            notes: list[str] = []
            if stale_reason is not None:
                if self.policy.defaults.stale_action == "drop":
                    withhold(stale_reason, source.id)
                    continue
                notes.append(explain(stale_reason))

            redacted = self._redact(record, verdict.redact_fields)
            if verdict.decision is Decision.REDACT and verdict.redact_fields:
                notes.append(
                    f"redacted fields: {', '.join(verdict.redact_fields)}"
                )

            survivors.append(
                ResultRecord(
                    id=redacted.id,
                    source_id=source.id,
                    title=redacted.title,
                    text=redacted.text,
                    score=redacted.score,
                    citation=Citation(
                        record_id=redacted.id,
                        source_id=source.id,
                        source_title=source.title,
                        owner=source.owner,
                        updated_at=redacted.updated_at,
                        age_days=(
                            round(age, 1) if (age := redacted.age_days(now)) is not None else None
                        ),
                        sensitivity=redacted.sensitivity or source.sensitivity,
                    ),
                    redacted_fields=verdict.redact_fields,
                    notes=tuple(notes),
                )
            )

        survivors.sort(key=lambda r: -r.score)
        kept: list[ResultRecord] = []
        spent = 0
        for result in survivors:
            cost = estimate_tokens(result.text)
            if len(kept) >= max_records or spent + cost > token_budget:
                withhold(Reason.BUDGET_TRUNCATED, result.source_id)
                continue
            kept.append(result)
            spent += cost

        groups = [
            WithheldGroup(
                reason=reason,
                explanation=explain(reason),
                count=count,
                sources=tuple(sorted(withheld[reason])),
            )
            for reason, count in sorted(withheld_counts.items(), key=lambda kv: -kv[1])
        ]
        return EnforcementOutcome(kept=kept, withheld=groups)

    # -- gates ------------------------------------------------------------ #

    def _screen(
        self,
        principal: Principal,
        purpose: str,
        source: Source,
        record: Record,
        now: datetime | None,
    ) -> Reason | None:
        """Run identity and clearance gates. Returns a reason when the record fails."""
        if record.tenant is not None and record.tenant != principal.tenant:
            return Reason.TENANT_MISMATCH

        acl = record.acl
        if acl is None:
            default_acl = source.config.get("default_acl")
            if not default_acl:
                return Reason.MISSING_ACL
            acl = tuple(default_acl)
        if not principal.matches_acl(acl):
            return Reason.ACL_MISMATCH

        sensitivity: Sensitivity = record.sensitivity or source.sensitivity
        if sensitivity_rank(sensitivity) > sensitivity_rank(principal.clearance):
            return Reason.INSUFFICIENT_CLEARANCE

        return None

    @staticmethod
    def _freshness(source: Source, record: Record, now: datetime | None) -> Reason | None:
        """Return a freshness reason when the record violates the source SLA."""
        if source.freshness_sla_days is None:
            return None
        age = record.age_days(now)
        if age is None:
            return Reason.MISSING_TIMESTAMP
        if age > source.freshness_sla_days:
            return Reason.STALE
        return None

    @staticmethod
    def _redact(record: Record, fields: tuple[str, ...]) -> Record:
        """Remove redacted fields from structured data and scrub them from text.

        Scrubbing the text as well as the field matters: a salary that also appears
        in a free-text note is still a leaked salary.
        """
        if not fields:
            return record

        remaining = dict(record.fields)
        text = record.text
        for name in fields:
            value = remaining.pop(name, None)
            if value is not None:
                rendered = str(value)
                if rendered and rendered in text:
                    text = text.replace(rendered, REDACTION_MARKER)
            remaining[name] = REDACTION_MARKER

        return record.model_copy(update={"fields": remaining, "text": text})
