"""Declarative policy engine.

Policy answers one question: may this principal, acting for this declared purpose,
read this source (and this record)? The engine is deliberately small and total -
every path returns a decision with a reason code, and every failure denies.

Evaluation semantics:

* **Default deny.** A request with no matching allow rule is denied.
* **Deny overrides.** An explicit deny beats any number of allows.
* **Redactions accumulate.** All matching redact rules contribute their fields.

Action rules (v2) are kept strictly separate from read rules: a rule that names
actions governs only actions, and a rule that does not is invisible to action
evaluation. Read access can therefore never accumulate into permission to act.
Action grants are alternatives - the impact limits on each grant decide which one
covers a given proposal - while denies remain absolute.

The same rule set is evaluated twice per query: once per source to decide
eligibility, then once per candidate record. Rules that reference record-only
dimensions are simply inert at the source stage.
"""

from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .reasons import Reason
from .types import Decision, Principal, Record, Sensitivity, Source, sensitivity_rank


class RuleMatch(BaseModel):
    """The conditions under which a rule applies.

    Every populated dimension must match (logical AND). An empty dimension is a
    wildcard for that dimension.
    """

    model_config = ConfigDict(frozen=True)

    principals: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    tenants: tuple[str, ...] = ()
    purposes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    sensitivity_at_least: Sensitivity | None = None
    sensitivity_at_most: Sensitivity | None = None

    @field_validator(
        "principals", "groups", "tenants", "purposes", "sources", "actions", "tags",
        mode="before",
    )
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        if isinstance(v, (list, set)):
            return tuple(v)
        return v


class Rule(BaseModel):
    """A single policy statement."""

    model_config = ConfigDict(frozen=True)

    id: str
    effect: Decision
    when: RuleMatch = Field(default_factory=RuleMatch)
    redact_fields: tuple[str, ...] = ()
    reason: Reason | None = None
    description: str = ""

    # Action-only controls. Ignored when the rule does not name any action.
    requires_approval: bool = False
    max_amount: float | None = None
    max_affected: int | None = None
    allow_irreversible: bool = True

    # Aggregate ceilings over a rolling window. Per-action limits alone leave the
    # obvious hole open: a hundred separately-legal refunds are still a hundred
    # refunds. These bound the total, not the individual call.
    window_seconds: int = 86400
    max_amount_per_window: float | None = None
    max_actions_per_window: int | None = None

    @field_validator("redact_fields", mode="before")
    @classmethod
    def _coerce_fields(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        if isinstance(v, (list, set)):
            return tuple(v)
        return v


class PolicyDefaults(BaseModel):
    """Behavior for conditions no rule speaks to."""

    model_config = ConfigDict(frozen=True)

    stale_action: str = "tag"  # "tag" keeps the record and flags it; "drop" withholds it

    #: How long an approved proposal stays executable. Short by design: an
    #: approval is a decision about a moment, not a standing permission.
    proposal_ttl_seconds: int = 900

    @field_validator("stale_action")
    @classmethod
    def _check_stale(cls, v: str) -> str:
        if v not in {"tag", "drop"}:
            raise ValueError("stale_action must be 'tag' or 'drop'")
        return v


class Verdict(BaseModel):
    """Result of evaluating policy for one (principal, purpose, source[, record])."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    reason: Reason
    redact_fields: tuple[str, ...] = ()
    matched_rules: tuple[str, ...] = ()

    @property
    def permitted(self) -> bool:
        """True when the subject may be read (possibly with redactions)."""
        return self.decision in (Decision.ALLOW, Decision.REDACT)


class ActionVerdict(BaseModel):
    """Result of evaluating policy for one proposed action."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    reason: Reason
    requires_approval: bool = False
    matched_rules: tuple[str, ...] = ()

    @property
    def permitted(self) -> bool:
        """True when the action may proceed, possibly after human approval."""
        return self.decision is Decision.ALLOW


class Policy(BaseModel):
    """A complete, validated policy document."""

    model_config = ConfigDict(frozen=True)

    version: int = 1
    purposes: tuple[str, ...] = ()
    defaults: PolicyDefaults = Field(default_factory=PolicyDefaults)
    rules: tuple[Rule, ...] = ()

    @field_validator("purposes", mode="before")
    @classmethod
    def _coerce_purposes(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, (list, set)):
            return tuple(v)
        return v

    @field_validator("rules", mode="before")
    @classmethod
    def _coerce_rules(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    # -- loading ---------------------------------------------------------- #

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        """Build a policy from parsed YAML, raising on any structural problem.

        Callers must let this raise: an unparseable policy has to stop the server
        rather than degrade into an open one.
        """
        policy = cls.model_validate(data)
        seen: set[str] = set()
        for rule in policy.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id: {rule.id}")
            seen.add(rule.id)
        return policy

    # -- evaluation ------------------------------------------------------- #

    def knows_purpose(self, purpose: str) -> bool:
        """True if the purpose is registered. An empty registry accepts anything."""
        return not self.purposes or purpose in self.purposes

    def evaluate(
        self,
        principal: Principal,
        purpose: str,
        source: Source,
        record: Record | None = None,
    ) -> Verdict:
        """Decide access, returning a decision and the reason behind it.

        Any unexpected error is converted into a deny with ``policy_error``; the
        engine never propagates an exception to the caller, because a crashing
        policy check must not become an open door.
        """
        try:
            return self._evaluate(principal, purpose, source, record)
        except Exception:  # noqa: BLE001 - fail closed on any evaluation fault
            return Verdict(decision=Decision.DENY, reason=Reason.POLICY_ERROR)

    def _evaluate(
        self,
        principal: Principal,
        purpose: str,
        source: Source,
        record: Record | None,
    ) -> Verdict:
        if not self.knows_purpose(purpose):
            return Verdict(decision=Decision.DENY, reason=Reason.PURPOSE_NOT_PERMITTED)
        if not source.permits_purpose(purpose):
            return Verdict(decision=Decision.DENY, reason=Reason.PURPOSE_NOT_PERMITTED)

        effective_sensitivity = (
            record.sensitivity if record and record.sensitivity else source.sensitivity
        )
        tags = set(source.tags)
        if record:
            tags.update(str(t) for t in record.fields.get("tags", ()) or ())

        denies: list[Rule] = []
        allows: list[Rule] = []
        redacts: list[Rule] = []

        for rule in self.rules:
            # A rule that names actions governs actions only. Read access and action
            # authority are separate powers; letting one grant the other is exactly
            # the privilege bleed this split exists to prevent.
            if rule.when.actions:
                continue
            if not self._matches(rule.when, principal, purpose, source, effective_sensitivity, tags):
                continue
            if rule.effect is Decision.DENY:
                denies.append(rule)
            elif rule.effect is Decision.ALLOW:
                allows.append(rule)
            else:
                redacts.append(rule)

        if denies:
            first = denies[0]
            return Verdict(
                decision=Decision.DENY,
                reason=first.reason or Reason.EXPLICIT_DENY,
                matched_rules=tuple(r.id for r in denies),
            )

        if not allows:
            return Verdict(
                decision=Decision.DENY,
                reason=Reason.NO_MATCHING_RULE,
                matched_rules=(),
            )

        redact_fields = tuple(
            sorted({field for rule in redacts for field in rule.redact_fields})
        )
        matched = tuple(r.id for r in (*allows, *redacts))
        if redact_fields:
            return Verdict(
                decision=Decision.REDACT,
                reason=Reason.REDACTED,
                redact_fields=redact_fields,
                matched_rules=matched,
            )
        return Verdict(decision=Decision.ALLOW, reason=Reason.ALLOWED, matched_rules=matched)

    # -- action evaluation ------------------------------------------------ #

    def evaluate_action(
        self,
        principal: Principal,
        purpose: str,
        action_id: str,
        *,
        reversible: bool,
        amount: float = 0.0,
        affected: int = 0,
    ) -> ActionVerdict:
        """Decide whether a proposed action may run.

        Only rules that explicitly name actions are considered, so no amount of read
        access can add up to permission to act.

        The impact figures come from the executor's dry run, never from the agent.
        A model that under-reports the blast radius of its own action would otherwise
        be able to talk its way past a limit.
        """
        try:
            return self._evaluate_action(
                principal, purpose, action_id, reversible, amount, affected
            )
        except Exception:  # noqa: BLE001 - fail closed on any evaluation fault
            return ActionVerdict(decision=Decision.DENY, reason=Reason.POLICY_ERROR)

    def _evaluate_action(
        self,
        principal: Principal,
        purpose: str,
        action_id: str,
        reversible: bool,
        amount: float,
        affected: int,
    ) -> ActionVerdict:
        if not self.knows_purpose(purpose):
            return ActionVerdict(decision=Decision.DENY, reason=Reason.PURPOSE_NOT_PERMITTED)

        denies: list[Rule] = []
        allows: list[Rule] = []

        for rule in self.rules:
            if not rule.when.actions:
                continue
            if action_id not in rule.when.actions and "*" not in rule.when.actions:
                continue
            if not self._matches_identity(rule.when, principal, purpose):
                continue
            (denies if rule.effect is Decision.DENY else allows).append(rule)

        if denies:
            first = denies[0]
            return ActionVerdict(
                decision=Decision.DENY,
                reason=first.reason or Reason.EXPLICIT_DENY,
                matched_rules=tuple(rule.id for rule in denies),
            )
        if not allows:
            return ActionVerdict(decision=Decision.DENY, reason=Reason.ACTION_NOT_PERMITTED)

        # Grants are alternatives. A rule only counts if it tolerates this specific
        # impact, so tiers are expressible: "up to 100 freely" and "up to 5000 with
        # approval" are two grants, and which one applies depends on the amount.
        tolerating = [
            rule
            for rule in allows
            if self._tolerates(rule, reversible=reversible, amount=amount, affected=affected)
        ]

        if not tolerating:
            # Something granted the action but nothing tolerates this size of it.
            # Report the binding constraint rather than a bare denial.
            blocked_by_reversibility = any(
                not reversible and not rule.allow_irreversible for rule in allows
            )
            return ActionVerdict(
                decision=Decision.DENY,
                reason=(
                    Reason.IRREVERSIBLE_BLOCKED
                    if blocked_by_reversibility
                    else Reason.IMPACT_LIMIT_EXCEEDED
                ),
                matched_rules=tuple(rule.id for rule in allows),
            )

        # Approval is needed only when every grant that covers this impact demands it.
        # If one grant covers it freely, the agent has a path that needs no human.
        requires_approval = all(rule.requires_approval for rule in tolerating)
        return ActionVerdict(
            decision=Decision.ALLOW,
            reason=Reason.APPROVAL_REQUIRED if requires_approval else Reason.ALLOWED,
            requires_approval=requires_approval,
            matched_rules=tuple(rule.id for rule in tolerating),
        )

    @staticmethod
    def _tolerates(rule: Rule, *, reversible: bool, amount: float, affected: int) -> bool:
        """True when this grant covers an action of this size and reversibility."""
        if rule.max_amount is not None and amount > rule.max_amount:
            return False
        if rule.max_affected is not None and affected > rule.max_affected:
            return False
        if not reversible and not rule.allow_irreversible:
            return False
        return True

    @staticmethod
    def _matches_identity(match: RuleMatch, principal: Principal, purpose: str) -> bool:
        """Identity and purpose half of rule matching, shared by both evaluators."""
        if match.principals and principal.id not in match.principals:
            return False
        if match.groups and not any(group in set(principal.groups) for group in match.groups):
            return False
        if match.tenants and principal.tenant not in match.tenants:
            return False
        if match.purposes and purpose not in match.purposes:
            return False
        return True

    @staticmethod
    def _matches(
        match: RuleMatch,
        principal: Principal,
        purpose: str,
        source: Source,
        sensitivity: Sensitivity,
        tags: set[str],
    ) -> bool:
        """True when every populated dimension of the match applies."""

        def any_of(values: Iterable[str], candidates: set[str]) -> bool:
            return any(value in candidates for value in values)

        if match.principals and principal.id not in match.principals:
            return False
        if match.groups and not any_of(match.groups, set(principal.groups)):
            return False
        if match.tenants and principal.tenant not in match.tenants:
            return False
        if match.purposes and purpose not in match.purposes:
            return False
        if match.sources and source.id not in match.sources:
            return False
        if match.tags and not any_of(match.tags, tags):
            return False

        rank = sensitivity_rank(sensitivity)
        if match.sensitivity_at_least is not None:
            if rank < sensitivity_rank(match.sensitivity_at_least):
                return False
        if match.sensitivity_at_most is not None:
            if rank > sensitivity_rank(match.sensitivity_at_most):
                return False
        return True
