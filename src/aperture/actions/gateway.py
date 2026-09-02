"""The action gateway.

Same chokepoint as the read plane, extended to things that change the world:

    identity -> purpose -> action registered -> arguments valid -> blast radius
             -> policy -> approval -> execute -> compensation recorded -> lineage

Four properties are enforced here and tested as attacks in the red-team suite:

* **Read access never becomes action authority.** Policy keeps the two rule sets
  disjoint; the gateway only ever consults the action half.
* **Blast radius is measured by the executor**, from real state, before policy runs.
  The agent's description of its own action is never an input to the decision.
* **Approval is not a capability.** It is bound to one proposal, one argument hash,
  one proposer, and it expires. Policy is re-evaluated at execution time, so a
  permission revoked between approval and execution stops the action.
* **Every outcome is logged**, including refusals, in the same hash chain as reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..lineage import LineageLog
from ..policy import Policy
from ..reasons import Reason, explain
from ..types import Principal
from .catalog import ActionCatalog
from .executors import Executor, ExecutorError, build_executors
from .store import ActionStore, new_execution_id, new_proposal_id
from .types import (
    ActionRefusal,
    ActionSpec,
    ApprovalDecision,
    BlastRadius,
    ExecutionRecord,
    Proposal,
    ProposalState,
    hash_arguments,
)


class ActionGateway:
    """Governs everything an agent can do, as opposed to see."""

    def __init__(
        self,
        principals: Any,
        policy: Policy,
        catalog: ActionCatalog,
        store: ActionStore,
        lineage: LineageLog,
        workspace_root: Any,
    ) -> None:
        self.principals = principals
        self.policy = policy
        self.catalog = catalog
        self.store = store
        self.lineage = lineage
        self.executors: dict[str, Executor] = build_executors(workspace_root)

    # -- discovery -------------------------------------------------------- #

    def list_actions(self, principal_id: str, purpose: str) -> list[dict[str, Any]]:
        """Describe the actions this principal may take under this purpose.

        Actions the principal cannot take are omitted rather than listed as
        forbidden, for the same reason denied sources are hidden: the catalog is
        also a map of what the company can do.
        """
        principal = self.principals.get(principal_id)
        if principal is None:
            return []

        visible: list[dict[str, Any]] = []
        for spec in self.catalog:
            if not spec.permits_purpose(purpose):
                continue
            verdict = self.policy.evaluate_action(
                principal, purpose, spec.id, reversible=spec.reversible
            )
            if not verdict.permitted:
                continue
            visible.append(
                {
                    "id": spec.id,
                    "title": spec.title,
                    "description": spec.description,
                    "owner": spec.owner,
                    "effect_class": spec.effect_class,
                    "reversible": spec.reversible,
                    "requires_approval": verdict.requires_approval,
                    "parameters": {
                        name: parameter.model_dump() for name, parameter in spec.parameters.items()
                    },
                }
            )
        return visible

    # -- propose ---------------------------------------------------------- #

    def propose(
        self,
        principal_id: str,
        purpose: str,
        action_id: str,
        arguments: dict[str, Any],
    ) -> Proposal | ActionRefusal:
        """Price an action and decide whether it may run."""
        principal = self.principals.get(principal_id)
        if principal is None:
            return self._refuse(Reason.UNKNOWN_PRINCIPAL, principal_id, action_id=action_id)

        spec = self.catalog.get(action_id)
        if spec is None:
            return self._refuse(Reason.ACTION_NOT_REGISTERED, principal_id, action_id=action_id)

        if not spec.permits_purpose(purpose):
            return self._refuse(
                Reason.PURPOSE_NOT_PERMITTED, principal_id, action_id=action_id, purpose=purpose
            )

        invalid = self._validate_arguments(spec, arguments)
        if invalid:
            return self._refuse(
                Reason.INVALID_ARGUMENTS, principal_id, action_id=action_id,
                purpose=purpose, detail=invalid,
            )

        try:
            blast = self._estimate(spec, arguments)
        except (ExecutorError, KeyError, ValueError, TypeError) as exc:
            return self._refuse(
                Reason.EXECUTION_FAILED, principal_id, action_id=action_id,
                purpose=purpose, detail=str(exc),
            )

        verdict = self.policy.evaluate_action(
            principal,
            purpose,
            spec.id,
            reversible=spec.reversible,
            amount=blast.amount,
            affected=blast.affected,
        )
        if not verdict.permitted:
            return self._refuse(
                verdict.reason, principal_id, action_id=action_id, purpose=purpose,
                detail=blast.headline(),
            )

        proposal = Proposal(
            id=new_proposal_id(),
            created_at=datetime.now(timezone.utc),
            principal_id=principal.id,
            purpose=purpose,
            action_id=spec.id,
            arguments=arguments,
            arguments_hash=hash_arguments(arguments),
            blast=blast,
            state=(
                ProposalState.PENDING_APPROVAL
                if verdict.requires_approval
                else ProposalState.READY
            ),
            requires_approval=verdict.requires_approval,
            matched_rules=verdict.matched_rules,
        )
        self.store.save_proposal(proposal)
        self._log(
            "action_proposed",
            principal_id=principal.id,
            purpose=purpose,
            action_id=spec.id,
            proposal_id=proposal.id,
            blast=blast.model_dump(),
            state=str(proposal.state),
            matched_rules=list(verdict.matched_rules),
        )
        return proposal

    # -- approve ---------------------------------------------------------- #

    def decide(
        self,
        proposal_id: str,
        approver_id: str,
        approved: bool,
        note: str = "",
    ) -> Proposal | ActionRefusal:
        """Record a human decision on a pending proposal."""
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return self._refuse(Reason.PROPOSAL_NOT_FOUND, approver_id, proposal_id=proposal_id)

        approver = self.principals.get(approver_id)
        if approver is None:
            return self._refuse(Reason.UNKNOWN_PRINCIPAL, approver_id, proposal_id=proposal_id)

        if approver.id == proposal.principal_id:
            # Separation of duties. An agent that could approve its own proposal
            # would make the approval step decorative.
            return self._refuse(
                Reason.SELF_APPROVAL_FORBIDDEN, approver_id, proposal_id=proposal_id
            )

        spec = self.catalog.get(proposal.action_id)
        if spec is None:
            return self._refuse(
                Reason.ACTION_NOT_REGISTERED, approver_id, proposal_id=proposal_id
            )

        if not self._may_approve(spec, approver):
            return self._refuse(
                Reason.APPROVER_NOT_AUTHORIZED, approver_id, proposal_id=proposal_id,
                action_id=spec.id,
            )

        if proposal.state is not ProposalState.PENDING_APPROVAL:
            return self._refuse(
                Reason.ALREADY_EXECUTED if proposal.state is ProposalState.EXECUTED
                else Reason.APPROVAL_MISSING,
                approver_id,
                proposal_id=proposal_id,
                detail=f"proposal is {proposal.state}",
            )

        decision = ApprovalDecision(
            approved=approved,
            decided_by=approver.id,
            decided_at=datetime.now(timezone.utc),
            note=note,
        )
        updated = proposal.model_copy(
            update={
                "approval": decision,
                "state": ProposalState.READY if approved else ProposalState.DENIED,
            }
        )
        self.store.save_proposal(updated)
        self._log(
            "action_decided",
            principal_id=approver.id,
            purpose=proposal.purpose,
            action_id=proposal.action_id,
            proposal_id=proposal.id,
            approved=approved,
            note=note,
            proposer=proposal.principal_id,
        )
        return updated

    # -- execute ---------------------------------------------------------- #

    def execute(
        self,
        proposal_id: str,
        principal_id: str,
        arguments: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ExecutionRecord | ActionRefusal:
        """Run a proposal that policy and any required human have cleared."""
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return self._refuse(Reason.PROPOSAL_NOT_FOUND, principal_id, proposal_id=proposal_id)

        principal = self.principals.get(principal_id)
        if principal is None:
            return self._refuse(Reason.UNKNOWN_PRINCIPAL, principal_id, proposal_id=proposal_id)

        if principal.id != proposal.principal_id:
            return self._refuse(
                Reason.ACTION_NOT_PERMITTED, principal_id, proposal_id=proposal_id,
                detail="a proposal may only be executed by the identity that made it",
            )

        if proposal.state is ProposalState.EXECUTED:
            return self._refuse(Reason.ALREADY_EXECUTED, principal_id, proposal_id=proposal_id)
        if proposal.state is ProposalState.DENIED:
            return self._refuse(Reason.APPROVAL_DENIED, principal_id, proposal_id=proposal_id)
        if proposal.state is ProposalState.PENDING_APPROVAL:
            return self._refuse(Reason.APPROVAL_MISSING, principal_id, proposal_id=proposal_id)

        if proposal.age_seconds(now) > self.policy.defaults.proposal_ttl_seconds:
            return self._refuse(Reason.PROPOSAL_EXPIRED, principal_id, proposal_id=proposal_id)

        if arguments is not None and hash_arguments(arguments) != proposal.arguments_hash:
            # The reviewed arguments are the approved arguments. Anything else is a
            # different action wearing an approved proposal's id.
            return self._refuse(
                Reason.ARGUMENTS_CHANGED, principal_id, proposal_id=proposal_id
            )

        spec = self.catalog.get(proposal.action_id)
        if spec is None:
            return self._refuse(
                Reason.ACTION_NOT_REGISTERED, principal_id, proposal_id=proposal_id
            )

        # Re-evaluate: permissions can change between approval and execution, and the
        # later decision is the one that counts.
        verdict = self.policy.evaluate_action(
            principal,
            proposal.purpose,
            spec.id,
            reversible=spec.reversible,
            amount=proposal.blast.amount,
            affected=proposal.blast.affected,
        )
        if not verdict.permitted:
            return self._refuse(
                verdict.reason, principal_id, proposal_id=proposal_id, action_id=spec.id
            )

        executor = self.executors.get(spec.executor)
        if executor is None:
            return self._refuse(
                Reason.EXECUTION_FAILED, principal_id, proposal_id=proposal_id,
                detail=f"no executor named {spec.executor}",
            )

        try:
            result, compensation = executor.execute(spec, proposal.arguments)
        except (ExecutorError, KeyError, ValueError, TypeError) as exc:
            return self._refuse(
                Reason.EXECUTION_FAILED, principal_id, proposal_id=proposal_id,
                action_id=spec.id, detail=str(exc),
            )

        record = ExecutionRecord(
            id=new_execution_id(),
            proposal_id=proposal.id,
            action_id=spec.id,
            principal_id=principal.id,
            executed_at=datetime.now(timezone.utc),
            result=result,
            compensation=compensation,
        )
        self.store.save_execution(record)
        self.store.save_proposal(
            proposal.model_copy(
                update={"state": ProposalState.EXECUTED, "execution_id": record.id}
            )
        )
        self._log(
            "action_executed",
            principal_id=principal.id,
            purpose=proposal.purpose,
            action_id=spec.id,
            proposal_id=proposal.id,
            execution_id=record.id,
            result=result,
            reversible=compensation is not None,
            approved_by=proposal.approval.decided_by if proposal.approval else None,
        )
        return record

    # -- rollback --------------------------------------------------------- #

    def rollback(self, execution_id: str, principal_id: str) -> ExecutionRecord | ActionRefusal:
        """Undo a previous execution, when the action recorded a way to undo it."""
        record = self.store.get_execution(execution_id)
        if record is None:
            return self._refuse(Reason.PROPOSAL_NOT_FOUND, principal_id, detail=execution_id)

        principal = self.principals.get(principal_id)
        if principal is None:
            return self._refuse(Reason.UNKNOWN_PRINCIPAL, principal_id)

        if record.compensation is None:
            return self._refuse(
                Reason.ROLLBACK_UNSUPPORTED, principal_id, action_id=record.action_id
            )
        if record.rolled_back_at is not None:
            return self._refuse(
                Reason.ALREADY_EXECUTED, principal_id, action_id=record.action_id,
                detail="this execution was already rolled back",
            )

        spec = self.catalog.get(record.action_id)
        executor = self.executors.get(spec.executor) if spec else None
        if spec is None or executor is None:
            return self._refuse(Reason.ACTION_NOT_REGISTERED, principal_id)

        try:
            rollback_result = executor.compensate(spec, record.compensation)
        except (ExecutorError, KeyError, ValueError, TypeError) as exc:
            return self._refuse(
                Reason.EXECUTION_FAILED, principal_id, action_id=spec.id, detail=str(exc)
            )

        updated = record.model_copy(
            update={
                "rolled_back_at": datetime.now(timezone.utc),
                "rollback_result": rollback_result,
            }
        )
        self.store.save_execution(updated)
        proposal = self.store.get_proposal(record.proposal_id)
        if proposal is not None:
            self.store.save_proposal(
                proposal.model_copy(update={"state": ProposalState.ROLLED_BACK})
            )
        self._log(
            "action_rolled_back",
            principal_id=principal.id,
            action_id=spec.id,
            proposal_id=record.proposal_id,
            execution_id=record.id,
            result=rollback_result,
        )
        return updated

    # -- internals -------------------------------------------------------- #

    def _estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        """Ask the executor to price the action, and stamp the catalog's reversibility."""
        executor = self.executors.get(spec.executor)
        if executor is None:
            raise ExecutorError(f"no executor named {spec.executor}")
        blast = executor.estimate(spec, arguments)
        return blast.model_copy(update={"reversible": spec.reversible})

    @staticmethod
    def _validate_arguments(spec: ActionSpec, arguments: dict[str, Any]) -> str:
        """Return a description of the first argument problem, or an empty string."""
        unknown = sorted(set(arguments) - set(spec.parameters))
        if unknown:
            return f"unknown argument(s): {', '.join(unknown)}"
        missing = sorted(
            name
            for name, parameter in spec.parameters.items()
            if parameter.required and name not in arguments
        )
        if missing:
            return f"missing required argument(s): {', '.join(missing)}"

        checks = {
            "number": (int, float),
            "integer": (int,),
            "boolean": (bool,),
            "string": (str,),
        }
        for name, value in arguments.items():
            expected = spec.parameters[name].type
            if not isinstance(value, checks[expected]) or (
                expected != "boolean" and isinstance(value, bool)
            ):
                return f"argument '{name}' must be a {expected}"
        return ""

    @staticmethod
    def _may_approve(spec: ActionSpec, approver: Principal) -> bool:
        """True when this identity is allowed to approve this action.

        The catalog names approver groups per action. An action with none named can
        be approved by any registered identity other than the proposer, which is a
        deliberately weak default: it is visible in the catalog and easy to tighten.
        """
        groups = spec.config.get("approver_groups")
        if not groups:
            return True
        return any(group in set(approver.groups) for group in groups)

    def _refuse(
        self,
        reason: Reason,
        principal_id: str,
        action_id: str | None = None,
        proposal_id: str | None = None,
        purpose: str = "",
        detail: str = "",
    ) -> ActionRefusal:
        """Build a refusal and record the attempt."""
        self._log(
            "action_refused",
            principal_id=principal_id,
            purpose=purpose,
            action_id=action_id,
            proposal_id=proposal_id,
            reason=str(reason),
            detail=detail,
        )
        return ActionRefusal(
            reason=reason,
            explanation=explain(reason),
            action_id=action_id,
            proposal_id=proposal_id,
            detail=detail,
        )

    def _log(self, kind: str, **payload: Any) -> None:
        """Append one entry to the shared lineage chain."""
        self.lineage.append({"kind": kind, **payload})
