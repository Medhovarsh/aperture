"""Policy conformance matrix.

This is the auditable heart of the test suite. It asserts, for every
(principal, purpose) pair the demo workspace defines, exactly which sources are
readable. A policy edit that widens access to a source someone should not see fails
here rather than in production.

Read the table as a specification: it is the shortest honest statement of who can
see what.
"""

from __future__ import annotations

import pytest

from aperture.plane import ContextPlane

# (principal, purpose) -> the complete set of source ids that must be readable.
CONFORMANCE: dict[tuple[str, str], set[str]] = {
    # People Ops: handbook and directory, only when acting for HR support.
    ("u_dana", "hr_support"): {"hr_handbook", "people_db"},
    ("u_dana", "customer_support"): {"support_kb"},
    ("u_dana", "engineering_oncall"): {"eng_runbooks"},
    # Declaring "security_audit" without the auditor group grants nothing extra:
    # only the internal sources any employee could already read.
    ("u_dana", "security_audit"): {"eng_runbooks", "support_kb"},
    # Platform engineer: on-call runbooks, never HR material.
    ("u_raj", "engineering_oncall"): {"eng_runbooks"},
    ("u_raj", "hr_support"): set(),
    ("u_raj", "customer_support"): {"support_kb"},
    # Support lead and the support service account behave identically.
    ("u_kim", "customer_support"): {"support_kb"},
    ("u_kim", "hr_support"): set(),
    ("svc_support_agent", "customer_support"): {"support_kb"},
    ("svc_support_agent", "hr_support"): set(),
    # Security audit sees everything, but only under the audit purpose.
    ("u_sam", "security_audit"): {"hr_handbook", "eng_runbooks", "support_kb", "people_db"},
    ("u_sam", "customer_support"): {"support_kb"},
    # Cross-tenant partner sees nothing of Acme's.
    ("u_partner", "customer_support"): set(),
    ("u_partner", "hr_support"): set(),
    ("u_partner", "security_audit"): set(),
}


@pytest.mark.parametrize(("key", "expected"), sorted(CONFORMANCE.items()))
def test_source_visibility_matches_specification(
    plane: ContextPlane, key: tuple[str, str], expected: set[str]
) -> None:
    principal_id, purpose = key
    visible = {source["id"] for source in plane.list_sources(principal_id, purpose)}
    assert visible == expected, (
        f"{principal_id} under '{purpose}' should see {sorted(expected)}, saw {sorted(visible)}"
    )


def test_unknown_principal_sees_nothing(plane: ContextPlane) -> None:
    assert plane.list_sources("u_does_not_exist", "hr_support") == []


def test_unregistered_purpose_yields_no_sources(plane: ContextPlane) -> None:
    """A purpose the policy does not define is not a loophole."""
    assert plane.list_sources("u_dana", "personal_curiosity") == []


def test_every_principal_and_purpose_pair_is_covered(plane: ContextPlane) -> None:
    """Guard against silent gaps: new identities must be added to the matrix.

    Service accounts and purposes get added to workspaces casually. Without this
    check, a new principal could ship entirely untested.
    """
    principals = set(plane.workspace.principals.ids())
    covered = {principal for principal, _ in CONFORMANCE}
    assert principals == covered, f"principals missing from the matrix: {principals - covered}"
