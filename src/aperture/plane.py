"""The context plane.

One class ties the pieces together and is the only thing callers - the MCP server,
the CLI, tests - talk to. The ordering below is the product:

    identity -> purpose -> source eligibility -> routing -> retrieval
             -> record enforcement -> budget -> lineage

Two invariants hold on every path through :meth:`ContextPlane.search`:

* **Nothing is withheld silently.** Every record removed at any stage contributes a
  reason code to the response.
* **Every query is logged before it is returned.** The caller cannot receive data
  that the lineage log does not know about.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime

from .brokers import Broker, BrokerError, build_brokers
from .enforcement import Enforcer
from .lineage import new_trace_id
from .observability import record_search
from .reasons import Reason, explain
from .router import RoutedSource, SemanticRouter
from .types import (
    Principal,
    Record,
    ResultRecord,
    SearchRequest,
    SearchResponse,
    Source,
    WithheldGroup,
)
from .workspace import Workspace

#: How many candidate records to pull from each routed source before enforcement.
CANDIDATES_PER_SOURCE = 12


class ContextPlane:
    """Governed read access over the workspace's registered sources."""

    def __init__(self, workspace: Workspace, max_sources: int = 3) -> None:
        self.workspace = workspace
        self.enforcer = Enforcer(workspace.policy)
        self.router = SemanticRouter()
        self.brokers: dict[str, Broker] = build_brokers(workspace.root)
        self.max_sources = max_sources

    # -- public API ------------------------------------------------------- #

    def list_sources(self, principal_id: str, purpose: str) -> list[dict[str, object]]:
        """Describe the sources this principal may read under this purpose.

        Sources the principal cannot read are omitted entirely rather than listed as
        forbidden: the catalog is also a namespace, and leaking source titles leaks
        organizational structure.
        """
        principal = self.workspace.principals.get(principal_id)
        if principal is None:
            return []
        eligible, _ = self._eligible_sources(principal, purpose)
        return [
            {
                "id": source.id,
                "title": source.title,
                "description": source.description,
                "owner": source.owner,
                "kind": source.kind,
                "sensitivity": str(source.sensitivity),
                "freshness_sla_days": source.freshness_sla_days,
            }
            for source in eligible
        ]

    def search(
        self,
        principal_id: str,
        request: SearchRequest,
        now: datetime | None = None,
    ) -> SearchResponse:
        """Answer a governed retrieval request."""
        started = time.perf_counter()
        trace_id = new_trace_id()
        principal = self.workspace.principals.get(principal_id)

        if principal is None:
            response = SearchResponse(
                trace_id=trace_id,
                withheld=[
                    WithheldGroup(
                        reason=Reason.UNKNOWN_PRINCIPAL,
                        explanation=explain(Reason.UNKNOWN_PRINCIPAL),
                        count=0,
                    )
                ],
                principal_id=principal_id,
                purpose=request.purpose,
            )
            self._log(trace_id, principal_id, None, request, response, [])
            return response

        eligible, source_withheld = self._eligible_sources(principal, request.purpose)
        if request.source_ids:
            requested = set(request.source_ids)
            eligible = [source for source in eligible if source.id in requested]

        routed = self.router.route(request.question, eligible, limit=self.max_sources)
        routed_by_id = {r.source_id: r for r in routed}

        candidates: list[tuple[Source, Record]] = []
        failed: list[str] = []
        for choice in routed:
            source = self.workspace.catalog.require(choice.source_id)
            broker = self.brokers.get(source.kind)
            if broker is None:
                failed.append(source.id)
                continue
            try:
                records = broker.search(source, request.question, CANDIDATES_PER_SOURCE)
            except BrokerError:
                failed.append(source.id)
                continue
            candidates.extend((source, record) for record in records)

        outcome = self.enforcer.apply(
            principal=principal,
            purpose=request.purpose,
            candidates=candidates,
            max_records=request.max_records,
            token_budget=request.token_budget,
            now=now,
        )

        withheld = _merge_withheld(source_withheld + outcome.withheld)
        if failed:
            withheld = _merge_withheld(
                withheld
                + [
                    WithheldGroup(
                        reason=Reason.SOURCE_UNAVAILABLE,
                        explanation=explain(Reason.SOURCE_UNAVAILABLE),
                        count=len(failed),
                        sources=tuple(sorted(failed)),
                    )
                ]
            )

        response = SearchResponse(
            trace_id=trace_id,
            records=outcome.kept,
            withheld=withheld,
            sources_consulted=[choice.source_id for choice in routed],
            sources_failed=failed,
            partial=bool(failed) or bool(withheld),
            principal_id=principal.id,
            purpose=request.purpose,
        )
        self._log(trace_id, principal_id, principal, request, response, list(routed_by_id.values()))
        record_search(
            purpose=request.purpose,
            returned=len(response.records),
            withheld=response.withheld,
            duration=time.perf_counter() - started,
        )
        return response

    def fetch(
        self,
        principal_id: str,
        source_id: str,
        record_id: str,
        purpose: str,
        now: datetime | None = None,
    ) -> ResultRecord | WithheldGroup:
        """Return one record by id, or the reason it cannot be returned.

        Fetch runs the same gates as search. A record id obtained from a previous
        response is not a capability: permissions are re-evaluated every time.
        """
        principal = self.workspace.principals.get(principal_id)
        if principal is None:
            return WithheldGroup(
                reason=Reason.UNKNOWN_PRINCIPAL,
                explanation=explain(Reason.UNKNOWN_PRINCIPAL),
                count=1,
            )

        source = self.workspace.catalog.get(source_id)
        if source is None:
            return WithheldGroup(
                reason=Reason.SOURCE_NOT_ELIGIBLE,
                explanation="source is not registered in the catalog",
                count=1,
            )

        verdict = self.workspace.policy.evaluate(principal, purpose, source, None)
        if not verdict.permitted:
            return WithheldGroup(
                reason=verdict.reason, explanation=explain(verdict.reason), count=1,
                sources=(source_id,),
            )

        broker = self.brokers.get(source.kind)
        if broker is None:
            return WithheldGroup(
                reason=Reason.SOURCE_UNAVAILABLE,
                explanation=explain(Reason.SOURCE_UNAVAILABLE),
                count=1,
                sources=(source_id,),
            )
        try:
            record = broker.fetch(source, record_id)
        except BrokerError:
            return WithheldGroup(
                reason=Reason.SOURCE_UNAVAILABLE,
                explanation=explain(Reason.SOURCE_UNAVAILABLE),
                count=1,
                sources=(source_id,),
            )
        if record is None:
            return WithheldGroup(
                reason=Reason.NO_MATCHING_RULE,
                explanation="no such record in this source",
                count=1,
                sources=(source_id,),
            )

        outcome = self.enforcer.apply(
            principal=principal,
            purpose=purpose,
            candidates=[(source, record)],
            max_records=1,
            token_budget=10**9,
            now=now,
        )
        if outcome.kept:
            return outcome.kept[0]
        return outcome.withheld[0]

    def explain(self, trace_id: str) -> dict[str, object] | None:
        """Return the lineage entry for a trace id."""
        return self.workspace.lineage.find(trace_id)

    # -- internals -------------------------------------------------------- #

    def _eligible_sources(
        self, principal: Principal, purpose: str
    ) -> tuple[list[Source], list[WithheldGroup]]:
        """Split the catalog into readable sources and reasons for the rest."""
        eligible: list[Source] = []
        blocked: dict[Reason, list[str]] = defaultdict(list)

        for source in self.workspace.catalog:
            verdict = self.workspace.policy.evaluate(principal, purpose, source, None)
            if verdict.permitted:
                eligible.append(source)
            else:
                blocked[verdict.reason].append(source.id)

        withheld = [
            WithheldGroup(
                reason=reason,
                explanation=explain(reason),
                count=len(source_ids),
                sources=tuple(sorted(source_ids)),
            )
            for reason, source_ids in blocked.items()
        ]
        return eligible, withheld

    def _log(
        self,
        trace_id: str,
        principal_id: str,
        principal: Principal | None,
        request: SearchRequest,
        response: SearchResponse,
        routed: list[RoutedSource],
    ) -> None:
        """Append the lineage entry for one query."""
        self.workspace.lineage.append(
            {
                "trace_id": trace_id,
                "principal_id": principal_id,
                "tenant": principal.tenant if principal else None,
                "purpose": request.purpose,
                "question": request.question,
                "routing": [choice.model_dump() for choice in routed],
                "returned": [
                    {"record_id": r.id, "source_id": r.source_id, "score": r.score}
                    for r in response.records
                ],
                "withheld": [
                    {"reason": str(g.reason), "count": g.count, "sources": list(g.sources)}
                    for g in response.withheld
                ],
                "sources_failed": response.sources_failed,
                "partial": response.partial,
            }
        )


def _merge_withheld(groups: list[WithheldGroup]) -> list[WithheldGroup]:
    """Combine withheld groups that share a reason code."""
    counts: dict[Reason, int] = defaultdict(int)
    sources: dict[Reason, set[str]] = defaultdict(set)
    for group in groups:
        counts[group.reason] += group.count
        sources[group.reason].update(group.sources)
    return [
        WithheldGroup(
            reason=reason,
            explanation=explain(reason),
            count=count,
            sources=tuple(sorted(sources[reason])),
        )
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
