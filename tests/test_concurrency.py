"""Concurrency guarantees.

These are regression tests for a real defect. An earlier file-backed store did
read-modify-write, so eight concurrent ``execute`` calls on one approved 50 USD
refund all passed the state check before any of them wrote back, and eight refunds
were issued. The store is now SQLite and execution begins with an atomic claim.

Threads make these tests slower and less deterministic than the rest of the suite.
They are worth it: this class of bug is invisible to single-threaded tests and
expensive in production, because the thing being duplicated is money.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aperture.actions.gateway import ActionGateway
from aperture.actions.types import ActionRefusal, ExecutionRecord, Proposal, ProposalState
from aperture.reasons import Reason
from aperture.workspace import Workspace

THREADS = 8


@pytest.fixture()
def gateway(workspace: Workspace) -> ActionGateway:
    return workspace.gateway()


def ops_rows(workspace: Workspace, query: str) -> list[tuple]:
    connection = sqlite3.connect(workspace.root / "data" / "ops.db")
    try:
        return list(connection.execute(query))
    finally:
        connection.close()


def test_concurrent_execute_issues_exactly_one_refund(
    gateway: ActionGateway, workspace: Workspace
) -> None:
    """The double-spend regression, in its original shape."""
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.refund",
        {"customer_id": "cus-4471", "amount": 50.0},
    )
    assert isinstance(proposal, Proposal)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(
            pool.map(lambda _: gateway.execute(proposal.id, "svc_support_agent"), range(THREADS))
        )

    executed = [r for r in results if isinstance(r, ExecutionRecord)]
    refused = [r for r in results if isinstance(r, ActionRefusal)]

    assert len(executed) == 1
    assert len(refused) == THREADS - 1
    assert all(
        r.reason in {Reason.PROPOSAL_IN_FLIGHT, Reason.ALREADY_EXECUTED} for r in refused
    )

    rows = ops_rows(workspace, "SELECT amount FROM refunds")
    assert rows == [(50.0,)], f"expected exactly one refund row, got {rows}"


def test_concurrent_rollback_reverses_once(
    gateway: ActionGateway, workspace: Workspace
) -> None:
    """Two callers must not both reverse the same refund."""
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.refund",
        {"customer_id": "cus-5510", "amount": 25.0},
    )
    record = gateway.execute(proposal.id, "svc_support_agent")
    assert isinstance(record, ExecutionRecord)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(lambda _: gateway.rollback(record.id, "u_kim"), range(THREADS)))

    succeeded = [r for r in results if isinstance(r, ExecutionRecord)]
    assert len(succeeded) == 1
    assert ops_rows(workspace, "SELECT status FROM refunds") == [("reversed",)]


def test_concurrent_proposals_all_succeed(gateway: ActionGateway) -> None:
    """Proposing is a read plus a write of new state; it must not serialize into failure."""
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(
            pool.map(
                lambda i: gateway.propose(
                    "svc_support_agent", "customer_support", "support.refund",
                    {"customer_id": "cus-5510", "amount": 10.0},
                ),
                range(THREADS),
            )
        )
    proposals = [r for r in results if isinstance(r, Proposal)]
    assert len(proposals) == THREADS
    assert len({p.id for p in proposals}) == THREADS


def test_a_failed_executor_leaves_the_proposal_for_a_human(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An action whose outcome is unknown must not be auto-retried.

    Releasing the claim would invite a retry that double-charges; the proposal is
    parked in `executing` for an operator instead.
    """
    gateway = workspace.gateway()
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.refund",
        {"customer_id": "cus-5510", "amount": 10.0},
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("payment provider timed out")

    monkeypatch.setattr(gateway.executors["refund"], "execute", explode)
    refusal = gateway.execute(proposal.id, "svc_support_agent")
    assert isinstance(refusal, ActionRefusal)
    assert refusal.reason is Reason.EXECUTION_FAILED

    stored = gateway.store.get_proposal(proposal.id)
    assert stored.state is ProposalState.EXECUTING

    # A retry is refused rather than silently re-running the payment.
    assert gateway.execute(proposal.id, "svc_support_agent").reason is Reason.PROPOSAL_IN_FLIGHT


def test_stranded_proposals_are_surfaced_to_operators(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = workspace.gateway()
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.refund",
        {"customer_id": "cus-5510", "amount": 10.0},
    )
    monkeypatch.setattr(
        gateway.executors["refund"], "execute",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    gateway.execute(proposal.id, "svc_support_agent")

    stranded = workspace.action_store.stuck_executions(older_than_seconds=-1)
    assert [p.id for p in stranded] == [proposal.id]
