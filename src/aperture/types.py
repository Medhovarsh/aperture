"""Core domain models for the context plane."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .reasons import Reason

# --------------------------------------------------------------------------- #
# Sensitivity
# --------------------------------------------------------------------------- #


class Sensitivity(str, Enum):
    """Data classification. Ordered; compare with :func:`sensitivity_rank`."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    def __str__(self) -> str:
        return self.value


_SENSITIVITY_ORDER: dict[str, int] = {
    Sensitivity.PUBLIC.value: 0,
    Sensitivity.INTERNAL.value: 1,
    Sensitivity.CONFIDENTIAL.value: 2,
    Sensitivity.RESTRICTED.value: 3,
}


def sensitivity_rank(value: "Sensitivity | str") -> int:
    """Return the ordinal rank of a sensitivity label (higher is more restricted)."""
    return _SENSITIVITY_ORDER[Sensitivity(value).value]


class Decision(str, Enum):
    """Outcome of a policy evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    REDACT = "redact"

    def __str__(self) -> str:
        return self.value


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class Principal(BaseModel):
    """Who is asking.

    A principal is a human, a service, or an agent acting on behalf of one. Aperture
    never infers identity from prompt text; it comes from server configuration or a
    signed caller assertion.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str = ""
    tenant: str
    groups: tuple[str, ...] = ()
    clearance: Sensitivity = Sensitivity.INTERNAL
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("groups", mode="before")
    @classmethod
    def _coerce_groups(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, (list, set)):
            return tuple(v)
        return v

    def matches_acl(self, acl: tuple[str, ...]) -> bool:
        """True if this principal appears in an ACL entry list.

        A star entry is a wildcard. Otherwise an entry matches the principal id or
        any of its groups.
        """
        if "*" in acl:
            return True
        identifiers = {self.id, *self.groups}
        return any(entry in identifiers for entry in acl)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

SourceKind = Literal["docs", "sql", "vector"]


class Source(BaseModel):
    """A registered data source.

    The description field is load-bearing: the semantic router matches questions
    against it, so it should read like "what questions this source can answer".
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: SourceKind
    title: str
    description: str
    owner: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    freshness_sla_days: int | None = None
    allowed_purposes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_purposes", "tags", mode="before")
    @classmethod
    def _coerce_tuple(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, (list, set)):
            return tuple(v)
        return v

    def permits_purpose(self, purpose: str) -> bool:
        """True if the source may be read under the given purpose.

        An empty allowed_purposes list means the source is purpose-agnostic.
        """
        return not self.allowed_purposes or purpose in self.allowed_purposes


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


class Record(BaseModel):
    """A unit of retrievable content returned by a broker.

    Brokers populate acl, tenant, and updated_at from the source system. Missing
    metadata is treated as most restrictive rather than most permissive - see
    :mod:`aperture.enforcement`.
    """

    id: str
    source_id: str
    title: str = ""
    text: str
    tenant: str | None = None
    acl: tuple[str, ...] | None = None
    sensitivity: Sensitivity | None = None
    updated_at: datetime | None = None
    score: float = 0.0
    fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("acl", mode="before")
    @classmethod
    def _coerce_acl(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (list, set)):
            return tuple(v)
        if isinstance(v, str):
            return (v,)
        return v

    @field_validator("updated_at", mode="before")
    @classmethod
    def _coerce_ts(cls, v: Any) -> Any:
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v

    def age_days(self, now: datetime | None = None) -> float | None:
        """Age of this record in days, or None when it carries no timestamp."""
        if self.updated_at is None:
            return None
        reference = now or datetime.now(timezone.utc)
        stamp = self.updated_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (reference - stamp).total_seconds() / 86400.0


# --------------------------------------------------------------------------- #
# Request / response
# --------------------------------------------------------------------------- #


class Citation(BaseModel):
    """Provenance attached to every returned record."""

    record_id: str
    source_id: str
    source_title: str
    owner: str
    updated_at: datetime | None = None
    age_days: float | None = None
    sensitivity: Sensitivity


class ResultRecord(BaseModel):
    """A record that survived enforcement, plus what happened to it."""

    id: str
    source_id: str
    title: str
    text: str
    score: float
    citation: Citation
    redacted_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class WithheldGroup(BaseModel):
    """Aggregated account of what was withheld and why.

    This is the payload that makes denial explainable. Agents are expected to relay
    it to end users rather than silently answering from a partial context.
    """

    reason: Reason
    explanation: str
    count: int
    sources: tuple[str, ...] = ()


class SearchRequest(BaseModel):
    """An agent question, bound to a declared purpose."""

    question: str
    purpose: str
    max_records: int = 8
    token_budget: int = 6000
    source_ids: tuple[str, ...] | None = None


class SearchResponse(BaseModel):
    """What the plane returns: content, provenance, and an honest account of gaps."""

    trace_id: str
    records: list[ResultRecord] = Field(default_factory=list)
    withheld: list[WithheldGroup] = Field(default_factory=list)
    sources_consulted: list[str] = Field(default_factory=list)
    sources_failed: list[str] = Field(default_factory=list)
    partial: bool = False
    principal_id: str = ""
    purpose: str = ""

    def summary_line(self) -> str:
        """One-line human summary, suitable for an agent to relay verbatim."""
        parts = [f"{len(self.records)} record(s) returned"]
        if self.withheld:
            total = sum(group.count for group in self.withheld)
            detail = "; ".join(f"{g.count} {g.reason}" for g in self.withheld)
            parts.append(f"{total} withheld ({detail})")
        if self.sources_failed:
            parts.append(f"sources unavailable: {', '.join(self.sources_failed)}")
        return " | ".join(parts)
