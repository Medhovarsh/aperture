"""Action gateway behavior."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aperture.actions.catalog import ActionCatalog, ActionCatalogError
from aperture.actions.gateway import ActionGateway
from aperture.actions.types import ActionRefusal, ExecutionRecord, Proposal, ProposalState
from aperture.reasons import Reason
from aperture.workspace import Workspace


@pytest.fixture()
def gateway(workspace: Workspace) -> ActionGateway:
    return workspace.gateway()


def ops_rows(workspace: Workspace, query: str, args: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(workspace.root / "data" / "ops.db")
    try:
        return list(connection.execute(query, args))
    finally:
        connection.close()


def propose(gateway: ActionGateway, **kwargs) -> Proposal | ActionRefusal:
    defaults = {
        "principal_id": "svc_support_agent",
        "purpose": "customer_support",
        "action_id": "support.refund",
        "arguments": {"customer_id": "cus-5510", "amount": 50.0},
    }
    defaults.update(kwargs)
    return gateway.propose(**defaults)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_lists_only_actions_the_principal_may_take(gateway: ActionGateway) -> None:
    support = {a["id"] for a in gateway.list_actions("svc_support_agent", "customer_support")}
    assert support == {"support.refund", "support.close_ticket", "support.message_customer"}
    assert gateway.list_actions("u_dana", "customer_support") == []


def test_listing_reports_reversibility_and_approval(gateway: ActionGateway) -> None:
    by_id = {a["id"]: a for a in gateway.list_actions("svc_support_agent", "customer_support")}
    assert by_id["support.close_ticket"]["reversible"] is True
    assert by_id["support.message_customer"]["reversible"] is False
    assert by_id["support.message_customer"]["requires_approval"] is True


# --------------------------------------------------------------------------- #
# propose
# --------------------------------------------------------------------------- #


def test_small_refund_is_ready_without_approval(gateway: ActionGateway) -> None:
    """Both refund grants cover 50 USD, and the one that waives approval wins.

    Grants are alternatives: if any grant covers this impact without demanding a
    human, the agent has a path that needs no human.
    """
    proposal = propose(gateway)
    assert isinstance(proposal, Proposal)
    assert proposal.state is ProposalState.READY
    assert "support-small-refunds" in proposal.matched_rules


def test_large_refund_requires_approval(gateway: ActionGateway) -> None:
    proposal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 3000.0})
    assert proposal.state is ProposalState.PENDING_APPROVAL
    assert proposal.matched_rules == ("support-large-refunds",)


def test_refund_beyond_every_grant_is_refused(gateway: ActionGateway) -> None:
    refusal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 9000.0})
    assert isinstance(refusal, ActionRefusal)
    assert refusal.reason is Reason.IMPACT_LIMIT_EXCEEDED


def test_blast_radius_is_measured_from_real_state(gateway: ActionGateway) -> None:
    """One short argument, seven deleted accounts: the gap is the point."""
    small = gateway.propose("u_ops", "data_retention", "ops.purge_region", {"region": "apac"})
    assert small.blast.affected == 1
    big = gateway.propose("u_ops", "data_retention", "ops.purge_region", {"region": "legacy"})
    assert isinstance(big, ActionRefusal)
    assert big.reason is Reason.IMPACT_LIMIT_EXCEEDED
    assert "7 record(s)" in big.detail


def test_unregistered_action_cannot_be_proposed(gateway: ActionGateway) -> None:
    refusal = propose(gateway, action_id="support.wire_transfer", arguments={})
    assert refusal.reason is Reason.ACTION_NOT_REGISTERED


def test_action_purpose_allowlist_is_enforced(gateway: ActionGateway) -> None:
    refusal = propose(gateway, purpose="hr_support")
    assert refusal.reason is Reason.PURPOSE_NOT_PERMITTED


def test_unknown_principal_cannot_propose(gateway: ActionGateway) -> None:
    assert propose(gateway, principal_id="mallory").reason is Reason.UNKNOWN_PRINCIPAL


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"customer_id": "cus-5510"}, "missing required"),
        ({"customer_id": "cus-5510", "amount": 50.0, "wire": True}, "unknown argument"),
        ({"customer_id": "cus-5510", "amount": "fifty"}, "must be a number"),
    ],
)
def test_arguments_are_validated_against_the_catalog(
    gateway: ActionGateway, arguments: dict, fragment: str
) -> None:
    refusal = propose(gateway, arguments=arguments)
    assert refusal.reason is Reason.INVALID_ARGUMENTS
    assert fragment in refusal.detail


def test_estimate_failure_is_reported_not_executed(gateway: ActionGateway) -> None:
    refusal = propose(gateway, arguments={"customer_id": "cus-0000", "amount": 10.0})
    assert refusal.reason is Reason.EXECUTION_FAILED
    assert "unknown customer" in refusal.detail


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #


def test_approval_flow_reaches_execution(gateway: ActionGateway, workspace: Workspace) -> None:
    proposal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 3000.0})
    approved = gateway.decide(proposal.id, "u_kim", True, note="verified")
    assert approved.state is ProposalState.READY
    assert approved.approval.decided_by == "u_kim"

    record = gateway.execute(proposal.id, "svc_support_agent")
    assert isinstance(record, ExecutionRecord)
    rows = ops_rows(workspace, "SELECT amount, status FROM refunds")
    assert rows == [(3000.0, "issued")]


def test_denied_proposal_cannot_execute(gateway: ActionGateway) -> None:
    proposal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 3000.0})
    gateway.decide(proposal.id, "u_kim", False, note="not a duplicate")
    refusal = gateway.execute(proposal.id, "svc_support_agent")
    assert refusal.reason is Reason.APPROVAL_DENIED


def test_approver_must_belong_to_a_declared_approver_group(gateway: ActionGateway) -> None:
    proposal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 3000.0})
    assert gateway.decide(proposal.id, "u_raj", True).reason is Reason.APPROVER_NOT_AUTHORIZED


def test_deciding_a_ready_proposal_is_refused(gateway: ActionGateway) -> None:
    ready = propose(gateway)
    assert gateway.decide(ready.id, "u_kim", True).reason is Reason.APPROVAL_MISSING


# --------------------------------------------------------------------------- #
# execute
# --------------------------------------------------------------------------- #


def test_execution_changes_real_state(gateway: ActionGateway, workspace: Workspace) -> None:
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.close_ticket",
        {"ticket_id": "tkt-1180"},
    )
    gateway.execute(proposal.id, "svc_support_agent")
    assert ops_rows(workspace, "SELECT status FROM tickets WHERE ticket_id = ?", ("tkt-1180",)) == [
        ("closed",)
    ]


def test_a_proposal_executes_only_once(gateway: ActionGateway) -> None:
    proposal = propose(gateway)
    assert isinstance(gateway.execute(proposal.id, "svc_support_agent"), ExecutionRecord)
    assert gateway.execute(proposal.id, "svc_support_agent").reason is Reason.ALREADY_EXECUTED


def test_pending_proposal_cannot_be_executed(gateway: ActionGateway) -> None:
    proposal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 3000.0})
    assert gateway.execute(proposal.id, "svc_support_agent").reason is Reason.APPROVAL_MISSING


def test_expired_proposal_is_refused(gateway: ActionGateway) -> None:
    """An approval is a decision about a moment, not a standing permission."""
    proposal = propose(gateway)
    later = datetime.now(timezone.utc) + timedelta(seconds=3600)
    assert gateway.execute(proposal.id, "svc_support_agent", now=later).reason is (
        Reason.PROPOSAL_EXPIRED
    )


def test_missing_proposal_is_refused(gateway: ActionGateway) -> None:
    assert gateway.execute("prp_nope", "svc_support_agent").reason is Reason.PROPOSAL_NOT_FOUND


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #


def test_rollback_restores_previous_state(gateway: ActionGateway, workspace: Workspace) -> None:
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.close_ticket",
        {"ticket_id": "tkt-1182"},
    )
    record = gateway.execute(proposal.id, "svc_support_agent")
    gateway.rollback(record.id, "u_kim")
    assert ops_rows(workspace, "SELECT status FROM tickets WHERE ticket_id = ?", ("tkt-1182",)) == [
        ("pending",)
    ]


def test_irreversible_action_cannot_be_rolled_back(gateway: ActionGateway) -> None:
    proposal = gateway.propose(
        "svc_support_agent", "customer_support", "support.message_customer",
        {"to": "a@b.example", "subject": "hi", "body": "hello"},
    )
    gateway.decide(proposal.id, "u_kim", True)
    record = gateway.execute(proposal.id, "svc_support_agent")
    assert gateway.rollback(record.id, "u_kim").reason is Reason.ROLLBACK_UNSUPPORTED


def test_rollback_happens_at_most_once(gateway: ActionGateway) -> None:
    proposal = propose(gateway)
    record = gateway.execute(proposal.id, "svc_support_agent")
    assert isinstance(gateway.rollback(record.id, "u_kim"), ExecutionRecord)
    assert gateway.rollback(record.id, "u_kim").reason is Reason.ALREADY_EXECUTED


# --------------------------------------------------------------------------- #
# catalog integrity
# --------------------------------------------------------------------------- #


def test_catalog_rejects_a_false_reversibility_claim() -> None:
    """An action that promises an undo it cannot deliver is the worst catalog entry."""
    with pytest.raises(ActionCatalogError, match="declared reversible"):
        ActionCatalog.from_dict(
            {
                "actions": [
                    {
                        "id": "bad.send",
                        "title": "Send",
                        "description": "x",
                        "executor": "message",
                        "owner": "o",
                        "effect_class": "external",
                        "reversible": True,
                    }
                ]
            }
        )


def test_catalog_rejects_an_unknown_executor() -> None:
    with pytest.raises(ActionCatalogError, match="unknown executor"):
        ActionCatalog.from_dict(
            {
                "actions": [
                    {
                        "id": "bad.thing",
                        "title": "Thing",
                        "description": "x",
                        "executor": "does_not_exist",
                        "owner": "o",
                        "effect_class": "write",
                    }
                ]
            }
        )


def test_policy_referencing_an_unregistered_action_stops_startup(
    workspace_root: Path,
) -> None:
    policy_path = workspace_root / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "actions: [support.refund]", "actions: [support.embezzle]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="unregistered action"):
        Workspace.load(workspace_root)


# --------------------------------------------------------------------------- #
# lineage
# --------------------------------------------------------------------------- #


def test_every_action_outcome_is_logged(gateway: ActionGateway, workspace: Workspace) -> None:
    proposal = propose(gateway, arguments={"customer_id": "cus-4471", "amount": 3000.0})
    gateway.decide(proposal.id, "u_kim", True)
    record = gateway.execute(proposal.id, "svc_support_agent")
    gateway.rollback(record.id, "u_kim")
    propose(gateway, arguments={"customer_id": "cus-4471", "amount": 9000.0})

    kinds = [entry.get("kind") for entry in workspace.lineage.read_all()]
    assert "action_proposed" in kinds
    assert "action_decided" in kinds
    assert "action_executed" in kinds
    assert "action_rolled_back" in kinds
    assert "action_refused" in kinds

    ok, problems = workspace.lineage.verify()
    assert ok, problems


def test_reads_and_actions_share_one_chain(workspace: Workspace) -> None:
    """An auditor reconstructing an incident needs both halves, in order."""
    from aperture.plane import ContextPlane
    from aperture.types import SearchRequest

    plane = ContextPlane(workspace)
    plane.search("u_kim", SearchRequest(question="refund window", purpose="customer_support"))
    workspace.gateway().propose(
        "svc_support_agent", "customer_support", "support.refund",
        {"customer_id": "cus-5510", "amount": 25.0},
    )

    entries = list(workspace.lineage.read_all())
    assert [entry.get("kind", "search") for entry in entries] == ["search", "action_proposed"]
    ok, _ = workspace.lineage.verify()
    assert ok


# --------------------------------------------------------------------------- #
# rolling-window budgets
# --------------------------------------------------------------------------- #


def test_spend_budget_stops_a_retry_loop(gateway: ActionGateway, workspace: Workspace) -> None:
    """Per-call limits alone leave the obvious hole: many separately-legal calls.

    The demo policy allows 100 USD per refund but only 500 USD per rolling hour.
    Five 90 USD refunds are individually fine and collectively at the ceiling.
    """
    outcomes = []
    for _ in range(7):
        proposal = propose(gateway, arguments={"customer_id": "cus-5510", "amount": 90.0})
        if isinstance(proposal, ActionRefusal):
            outcomes.append(proposal.reason)
            continue
        result = gateway.execute(proposal.id, "svc_support_agent")
        outcomes.append("executed" if isinstance(result, ExecutionRecord) else result.reason)

    assert outcomes[:5] == ["executed"] * 5
    assert outcomes[5:] == [Reason.SPEND_LIMIT_EXCEEDED] * 2
    assert len(ops_rows(workspace, "SELECT id FROM refunds")) == 5


def test_action_count_ceiling_is_enforced_independently(gateway: ActionGateway) -> None:
    """Five small refunds hit the count ceiling before the amount ceiling."""
    for _ in range(5):
        proposal = propose(gateway, arguments={"customer_id": "cus-5510", "amount": 1.0})
        gateway.execute(proposal.id, "svc_support_agent")

    refusal = propose(gateway, arguments={"customer_id": "cus-5510", "amount": 1.0})
    assert isinstance(refusal, ActionRefusal)
    assert refusal.reason is Reason.RATE_LIMIT_EXCEEDED


def test_budget_is_rechecked_at_execution_time(gateway: ActionGateway) -> None:
    """A proposal made under budget must not execute after the budget is spent."""
    early = propose(gateway, arguments={"customer_id": "cus-5510", "amount": 90.0})
    assert isinstance(early, Proposal)

    for _ in range(5):
        other = propose(gateway, arguments={"customer_id": "cus-5510", "amount": 90.0})
        if isinstance(other, Proposal):
            gateway.execute(other.id, "svc_support_agent")

    refusal = gateway.execute(early.id, "svc_support_agent")
    assert isinstance(refusal, ActionRefusal)
    assert refusal.reason is Reason.SPEND_LIMIT_EXCEEDED


def test_rolled_back_actions_still_consume_budget(
    gateway: ActionGateway, workspace: Workspace
) -> None:
    """An action performed and then reversed really did move money twice."""
    proposal = propose(gateway, arguments={"customer_id": "cus-5510", "amount": 90.0})
    record = gateway.execute(proposal.id, "svc_support_agent")
    gateway.rollback(record.id, "u_kim")

    spent, count = workspace.action_store.spend_since(
        "svc_support_agent", "support.refund",
        datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert spent == 90.0
    assert count == 1
