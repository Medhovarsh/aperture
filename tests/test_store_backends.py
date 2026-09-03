"""One conformance suite, run against every store backend.

The store's contract is small and its most important clause is a single sentence:
:meth:`claim_for_execution` succeeds for exactly one caller. A second backend is
only useful if it upholds that clause identically, so the tests are written once
and parameterized over backends rather than duplicated per implementation.

Postgres runs when ``APERTURE_TEST_POSTGRES_DSN`` points at a database, which CI
provides as a service container. Locally it skips, and the skip is visible rather
than silent - a backend that is never exercised is a backend that does not work.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from aperture.actions.store import SqliteActionStore, new_execution_id, new_proposal_id
from aperture.actions.types import (
    ApprovalDecision,
    BlastRadius,
    ExecutionRecord,
    Proposal,
    ProposalState,
)

POSTGRES_DSN = os.environ.get("APERTURE_TEST_POSTGRES_DSN")


def make_postgres_store():
    """Build a Postgres store with a clean schema, or skip."""
    if not POSTGRES_DSN:
        pytest.skip("set APERTURE_TEST_POSTGRES_DSN to exercise the Postgres backend")
    psycopg = pytest.importorskip("psycopg", reason="needs the [postgres] extra")
    from aperture.actions.postgres_store import PostgresActionStore

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DROP TABLE IF EXISTS proposals, executions, seen_nonces CASCADE"
            )
    return PostgresActionStore(POSTGRES_DSN)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    """Every test in this file runs against each backend."""
    if request.param == "sqlite":
        backend = SqliteActionStore(tmp_path / "state")
    else:
        backend = make_postgres_store()
    yield backend
    backend.close()


def make_proposal(**overrides) -> Proposal:
    payload = {
        "id": new_proposal_id(),
        "created_at": datetime.now(timezone.utc),
        "principal_id": "svc_support_agent",
        "purpose": "customer_support",
        "action_id": "support.refund",
        "arguments": {"customer_id": "cus-1", "amount": 50.0},
        "arguments_hash": "abc123",
        "blast": BlastRadius(summary="Refund 50 USD", affected=1, amount=50.0, reversible=True),
        "state": ProposalState.READY,
        "requires_approval": False,
        "matched_rules": ("support-small-refunds",),
    }
    payload.update(overrides)
    return Proposal(**payload)


def make_execution(proposal: Proposal, **overrides) -> ExecutionRecord:
    payload = {
        "id": new_execution_id(),
        "proposal_id": proposal.id,
        "action_id": proposal.action_id,
        "principal_id": proposal.principal_id,
        "executed_at": datetime.now(timezone.utc),
        "result": {"refund_id": 1},
        "compensation": {"refund_id": 1},
    }
    payload.update(overrides)
    return ExecutionRecord(**payload)


# --------------------------------------------------------------------------- #
# round trips
# --------------------------------------------------------------------------- #


def test_proposal_round_trip(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)

    loaded = store.get_proposal(proposal.id)
    assert loaded is not None
    assert loaded.id == proposal.id
    assert loaded.arguments == proposal.arguments
    assert loaded.blast.amount == 50.0
    assert loaded.matched_rules == ("support-small-refunds",)
    assert loaded.state is ProposalState.READY


def test_missing_proposal_returns_none(store) -> None:
    assert store.get_proposal("prp_does_not_exist") is None


def test_approval_survives_a_round_trip(store) -> None:
    proposal = make_proposal(state=ProposalState.PENDING_APPROVAL, requires_approval=True)
    store.save_proposal(proposal)
    approved = proposal.model_copy(
        update={
            "state": ProposalState.READY,
            "approval": ApprovalDecision(
                approved=True,
                decided_by="u_kim",
                decided_at=datetime.now(timezone.utc),
                note="verified",
            ),
        }
    )
    store.save_proposal(approved)

    loaded = store.get_proposal(proposal.id)
    assert loaded.approval.decided_by == "u_kim"
    assert loaded.approval.note == "verified"


def test_listing_filters_by_state(store) -> None:
    store.save_proposal(make_proposal(state=ProposalState.READY))
    store.save_proposal(make_proposal(state=ProposalState.PENDING_APPROVAL))
    store.save_proposal(make_proposal(state=ProposalState.PENDING_APPROVAL))

    assert len(store.list_proposals()) == 3
    assert len(store.list_proposals(state="pending_approval")) == 2


def test_execution_round_trip(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    record = make_execution(proposal)
    store.save_execution(record, amount=50.0, affected=1)

    loaded = store.get_execution(record.id)
    assert loaded is not None
    assert loaded.result == {"refund_id": 1}
    assert loaded.compensation == {"refund_id": 1}
    assert loaded.reversible is True


# --------------------------------------------------------------------------- #
# the clause that matters
# --------------------------------------------------------------------------- #


def test_claim_admits_exactly_one_caller(store) -> None:
    """The double-spend guard, per backend."""
    proposal = make_proposal()
    store.save_proposal(proposal)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_for_execution(proposal.id), range(8)))

    assert results.count(True) == 1
    assert store.get_proposal(proposal.id).state is ProposalState.EXECUTING


def test_claim_fails_when_not_ready(store) -> None:
    proposal = make_proposal(state=ProposalState.PENDING_APPROVAL)
    store.save_proposal(proposal)
    assert store.claim_for_execution(proposal.id) is False


def test_release_returns_a_claim(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    assert store.claim_for_execution(proposal.id) is True
    assert store.release_claim(proposal.id) is True
    assert store.get_proposal(proposal.id).state is ProposalState.READY


def test_rollback_claim_admits_exactly_one_caller(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    record = make_execution(proposal)
    store.save_execution(record)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_rollback(record.id), range(8)))

    assert results.count(True) == 1


def test_rollback_claim_refuses_irreversible_executions(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    record = make_execution(proposal, compensation=None)
    store.save_execution(record)
    assert store.claim_rollback(record.id) is False


def test_failed_compensation_releases_the_rollback_claim(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    record = make_execution(proposal)
    store.save_execution(record)

    assert store.claim_rollback(record.id) is True
    store.undo_rollback_claim(record.id)
    assert store.claim_rollback(record.id) is True


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #


def test_spend_aggregates_within_the_window(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    for amount in (10.0, 20.0, 30.0):
        store.save_execution(make_execution(proposal), amount=amount)

    spent, count = store.spend_since(
        "svc_support_agent", "support.refund", datetime.now(timezone.utc) - timedelta(hours=1)
    )
    assert spent == 60.0
    assert count == 3


def test_spend_excludes_older_executions(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    store.save_execution(
        make_execution(proposal, executed_at=datetime.now(timezone.utc) - timedelta(days=2)),
        amount=500.0,
    )
    store.save_execution(make_execution(proposal), amount=10.0)

    spent, count = store.spend_since(
        "svc_support_agent", "support.refund", datetime.now(timezone.utc) - timedelta(hours=1)
    )
    assert spent == 10.0
    assert count == 1


def test_spend_is_scoped_to_principal_and_action(store) -> None:
    proposal = make_proposal()
    store.save_proposal(proposal)
    store.save_execution(make_execution(proposal), amount=10.0)
    store.save_execution(make_execution(proposal, principal_id="u_kim"), amount=999.0)
    store.save_execution(make_execution(proposal, action_id="support.close_ticket"), amount=999.0)

    spent, count = store.spend_since(
        "svc_support_agent", "support.refund", datetime.now(timezone.utc) - timedelta(hours=1)
    )
    assert spent == 10.0
    assert count == 1


# --------------------------------------------------------------------------- #
# replay protection
# --------------------------------------------------------------------------- #


def test_a_nonce_is_accepted_once(store) -> None:
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert store.remember_nonce("jti-1", expires) is True
    assert store.remember_nonce("jti-1", expires) is False


def test_concurrent_nonce_use_admits_one_winner(store) -> None:
    """Two workers presenting the same token at once must not both be told it is fresh."""
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.remember_nonce("jti-race", expires), range(8)))
    assert results.count(True) == 1


def test_expired_nonces_are_reaped(store) -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.remember_nonce("jti-old", past)
    # Any later call sweeps expired rows, so the id becomes usable again. That is
    # correct: the assertion carrying it can no longer be valid anyway.
    store.remember_nonce("jti-other", datetime.now(timezone.utc) + timedelta(minutes=5))
    assert store.remember_nonce("jti-old", datetime.now(timezone.utc) + timedelta(minutes=5))


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #


def test_stranded_executions_are_reported(store) -> None:
    proposal = make_proposal(
        state=ProposalState.EXECUTING,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    store.save_proposal(proposal)
    stranded = store.stuck_executions(older_than_seconds=300)
    assert [p.id for p in stranded] == [proposal.id]


def test_recent_executions_are_not_reported_as_stranded(store) -> None:
    store.save_proposal(make_proposal(state=ProposalState.EXECUTING))
    assert store.stuck_executions(older_than_seconds=300) == []
