"""Domain models for action governance.

The read plane answers "what may this agent see?". The action gateway answers the
harder question: "what may this agent *do*, and what happens if it is wrong?"

Three ideas carry the design:

* **Propose, then execute.** An agent never acts in one step. It proposes, the
  gateway prices the blast radius, policy decides, and only then can it execute.
* **Blast radius is measured, not asserted.** The executor computes impact with a
  dry run against real state. A model that under-reports the damage its own action
  would do must not be able to talk its way past a limit.
* **Approval is not a capability.** An approved proposal is re-checked against
  policy at execution time, is bound to the exact arguments that were approved, and
  expires.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..reasons import Reason, explain

#: Broad classes of consequence, used for catalog readability and policy targeting.
EffectClass = Literal["write", "financial", "external", "destructive"]


class ProposalState(str, Enum):
    """Lifecycle of a proposed action."""

    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    # Claimed by exactly one caller and currently running. A proposal stuck here
    # after a crash needs a human: the action's outcome is unknown.
    EXECUTING = "executing"
    EXECUTED = "executed"
    DENIED = "denied"
    ROLLED_BACK = "rolled_back"

    def __str__(self) -> str:
        return self.value


class ParameterSpec(BaseModel):
    """One argument an action accepts."""

    model_config = ConfigDict(frozen=True)

    type: Literal["string", "number", "integer", "boolean"] = "string"
    description: str = ""
    required: bool = True


class ActionSpec(BaseModel):
    """A registered action.

    Registration is a governance act: an action absent from the catalog cannot be
    proposed, exactly as an unregistered source cannot be read.

    ``reversible`` is a claim the catalog makes; the gateway verifies it against the
    executor at load time, because an action that says it can be undone but has no
    compensating operation is the most dangerous entry a catalog can contain.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str
    executor: str
    owner: str
    effect_class: EffectClass
    reversible: bool = False
    allowed_purposes: tuple[str, ...] = ()
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_purposes", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, (list, set)):
            return tuple(v)
        return v

    def permits_purpose(self, purpose: str) -> bool:
        """True if the action may be taken under this purpose."""
        return not self.allowed_purposes or purpose in self.allowed_purposes


class BlastRadius(BaseModel):
    """What this action would touch, measured by a dry run."""

    model_config = ConfigDict(frozen=True)

    summary: str
    affected: int = 0
    amount: float = 0.0
    currency: str = "USD"
    external_recipients: tuple[str, ...] = ()
    reversible: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("external_recipients", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, (list, set)):
            return tuple(v)
        if isinstance(v, str):
            return (v,)
        return v

    def headline(self) -> str:
        """One line a human reviewer can decide from."""
        parts = [self.summary]
        if self.affected:
            parts.append(f"{self.affected} record(s) affected")
        if self.amount:
            parts.append(f"{self.amount:,.2f} {self.currency}")
        if self.external_recipients:
            parts.append(f"leaves the company to: {', '.join(self.external_recipients)}")
        parts.append("reversible" if self.reversible else "IRREVERSIBLE")
        return " | ".join(parts)


def hash_arguments(arguments: dict[str, Any]) -> str:
    """Stable hash binding a proposal to the exact arguments that were reviewed."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class ApprovalDecision(BaseModel):
    """A human's verdict on a proposal."""

    approved: bool
    decided_by: str
    decided_at: datetime
    note: str = ""


class Proposal(BaseModel):
    """A proposed action awaiting execution or approval."""

    id: str
    created_at: datetime
    principal_id: str
    purpose: str
    action_id: str
    arguments: dict[str, Any]
    arguments_hash: str
    blast: BlastRadius
    state: ProposalState
    requires_approval: bool
    matched_rules: tuple[str, ...] = ()
    approval: ApprovalDecision | None = None
    execution_id: str | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        """How long ago this proposal was created."""
        reference = now or datetime.now(timezone.utc)
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (reference - created).total_seconds()


class ExecutionRecord(BaseModel):
    """The result of running an action, plus how to undo it."""

    id: str
    proposal_id: str
    action_id: str
    principal_id: str
    executed_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)
    compensation: dict[str, Any] | None = None
    rolled_back_at: datetime | None = None
    rollback_result: dict[str, Any] | None = None

    @property
    def reversible(self) -> bool:
        """True when a compensating operation was recorded and not yet used."""
        return self.compensation is not None and self.rolled_back_at is None


class ActionRefusal(BaseModel):
    """A refusal to propose, approve, execute, or roll back - always with a reason."""

    model_config = ConfigDict(frozen=True)

    reason: Reason
    explanation: str = ""
    action_id: str | None = None
    proposal_id: str | None = None
    detail: str = ""

    @classmethod
    def of(cls, reason: Reason, **kwargs: Any) -> "ActionRefusal":
        """Build a refusal with the standard explanation for its reason code."""
        return cls(reason=reason, explanation=explain(reason), **kwargs)
