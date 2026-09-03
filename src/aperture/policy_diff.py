"""Effective-access diffing.

A policy file is a set of rules. What a reviewer actually needs to know is not
which lines changed but who can now reach something they could not reach before.
Those are different questions, and reading a YAML diff to answer the second one is
how permissions quietly widen.

This module computes the *effective* access matrix - every principal, every
purpose, every source and action they can reach - and diffs two of them. A rule
refactor that changes forty lines and grants nothing new shows up as no change. A
one-word edit that hands the support team the employee directory shows up as a
widening, and `aperture policy diff` exits non-zero so CI can refuse it.

That inversion is the point: the tool is indifferent to how the policy is written
and interested only in what it permits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .actions.catalog import ActionCatalog
from .catalog import Catalog
from .identity import PrincipalRegistry
from .policy import Policy


@dataclass(frozen=True)
class Grant:
    """One thing one identity can reach, under one purpose."""

    principal_id: str
    purpose: str
    kind: str  # "source" or "action"
    target: str

    def describe(self) -> str:
        """Render as a line a reviewer can scan."""
        return f"{self.principal_id} / {self.purpose} -> {self.kind} {self.target}"


@dataclass
class AccessMatrix:
    """Everything every principal can reach, under every purpose."""

    grants: set[Grant] = field(default_factory=set)

    def __len__(self) -> int:
        return len(self.grants)

    def for_principal(self, principal_id: str) -> set[Grant]:
        """Every grant held by one identity."""
        return {grant for grant in self.grants if grant.principal_id == principal_id}


def compute_access(
    policy: Policy,
    principals: PrincipalRegistry,
    catalog: Catalog,
    actions: ActionCatalog | None = None,
) -> AccessMatrix:
    """Enumerate every grant the policy implies.

    Enumeration rather than inference: each (principal, purpose, target) triple is
    put through the same evaluator the runtime uses. A separate model of what the
    rules "mean" would eventually disagree with the enforcement path, and the
    disagreement would be invisible until it mattered.
    """
    matrix = AccessMatrix()
    purposes = list(policy.purposes) or ["*"]

    for principal in principals:
        for purpose in purposes:
            for source in catalog:
                if policy.evaluate(principal, purpose, source, None).permitted:
                    matrix.grants.add(
                        Grant(principal.id, purpose, "source", source.id)
                    )
            for spec in actions or ():
                if not spec.permits_purpose(purpose):
                    continue
                verdict = policy.evaluate_action(
                    principal, purpose, spec.id, reversible=spec.reversible
                )
                if verdict.permitted:
                    matrix.grants.add(Grant(principal.id, purpose, "action", spec.id))
    return matrix


@dataclass
class AccessDiff:
    """What changed between two policies, in terms of who can reach what."""

    widened: list[Grant]
    narrowed: list[Grant]

    @property
    def changed(self) -> bool:
        """True when effective access is different at all."""
        return bool(self.widened or self.narrowed)

    def render(self) -> str:
        """Human-readable summary, widenings first because they carry the risk."""
        if not self.changed:
            return "No change to effective access."

        lines: list[str] = []
        if self.widened:
            lines.append(f"WIDENED ({len(self.widened)}) - access that did not exist before:")
            lines.extend(f"  + {grant.describe()}" for grant in sorted(
                self.widened, key=lambda g: (g.principal_id, g.purpose, g.kind, g.target)
            ))
        if self.narrowed:
            if lines:
                lines.append("")
            lines.append(f"NARROWED ({len(self.narrowed)}) - access that has been removed:")
            lines.extend(f"  - {grant.describe()}" for grant in sorted(
                self.narrowed, key=lambda g: (g.principal_id, g.purpose, g.kind, g.target)
            ))
        return "\n".join(lines)


def diff_access(before: AccessMatrix, after: AccessMatrix) -> AccessDiff:
    """Compare two access matrices.

    Widening and narrowing are reported separately and never netted off. A change
    that removes one grant and adds another is not "no change"; it is one of each,
    and the added one still needs review.
    """
    return AccessDiff(
        widened=sorted(after.grants - before.grants, key=lambda g: g.describe()),
        narrowed=sorted(before.grants - after.grants, key=lambda g: g.describe()),
    )
