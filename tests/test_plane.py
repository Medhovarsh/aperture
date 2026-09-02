"""End-to-end behavior of the context plane."""

from __future__ import annotations

from pathlib import Path

from aperture.plane import ContextPlane
from aperture.reasons import Reason
from aperture.router import SemanticRouter
from aperture.types import ResultRecord, SearchRequest, Source
from aperture.workspace import Workspace


def test_answers_a_question_from_the_right_source(plane: ContextPlane) -> None:
    response = plane.search(
        "u_dana", SearchRequest(question="how much parental leave do we offer", purpose="hr_support")
    )
    assert response.sources_consulted == ["hr_handbook"]
    assert response.records
    assert "18 weeks" in response.records[0].text


def test_every_record_carries_provenance(plane: ContextPlane) -> None:
    response = plane.search(
        "u_raj", SearchRequest(question="database failover replication lag", purpose="engineering_oncall")
    )
    assert response.records
    citation = response.records[0].citation
    assert citation.source_id == "eng_runbooks"
    assert citation.owner == "platform@acme.example"
    assert citation.age_days is not None


def test_unknown_principal_is_refused_with_a_reason(plane: ContextPlane) -> None:
    response = plane.search("nobody", SearchRequest(question="anything", purpose="hr_support"))
    assert response.records == []
    assert [group.reason for group in response.withheld] == [Reason.UNKNOWN_PRINCIPAL]


def test_withheld_groups_are_merged_by_reason(plane: ContextPlane) -> None:
    """The caller gets one line per reason, not one per source."""
    response = plane.search(
        "u_kim", SearchRequest(question="parental leave policy", purpose="customer_support")
    )
    codes = [group.reason for group in response.withheld]
    assert len(codes) == len(set(codes))


def test_partial_flag_is_set_when_anything_is_withheld(plane: ContextPlane) -> None:
    response = plane.search(
        "u_kim", SearchRequest(question="refund window", purpose="customer_support")
    )
    assert response.withheld
    assert response.partial is True


def test_broken_source_is_reported_not_hidden(workspace_root: Path) -> None:
    """A dead source must be visible as a gap, never as a quiet absence."""
    (workspace_root / "data" / "support" / "kb.jsonl").unlink()
    plane = ContextPlane(Workspace.load(workspace_root))
    response = plane.search(
        "u_kim", SearchRequest(question="refund window", purpose="customer_support")
    )
    assert response.sources_failed == ["support_kb"]
    assert any(group.reason is Reason.SOURCE_UNAVAILABLE for group in response.withheld)
    assert response.partial is True


def test_source_filter_narrows_but_cannot_widen(plane: ContextPlane) -> None:
    """Naming a source the caller may not read does not make it readable."""
    response = plane.search(
        "u_kim",
        SearchRequest(
            question="parental leave", purpose="customer_support", source_ids=("hr_handbook",)
        ),
    )
    assert response.records == []
    assert response.sources_consulted == []


def test_token_budget_limits_returned_content(plane: ContextPlane) -> None:
    generous = plane.search(
        "u_raj",
        SearchRequest(question="incident severity deploy rollback failover", purpose="engineering_oncall"),
    )
    tight = plane.search(
        "u_raj",
        SearchRequest(
            question="incident severity deploy rollback failover",
            purpose="engineering_oncall",
            token_budget=60,
        ),
    )
    assert len(tight.records) < len(generous.records)
    assert any(group.reason is Reason.BUDGET_TRUNCATED for group in tight.withheld)


def test_fetch_returns_a_permitted_record(plane: ContextPlane) -> None:
    result = plane.fetch("u_kim", "support_kb", "kb-refund-window", "customer_support")
    assert isinstance(result, ResultRecord)
    assert "30 days" in result.text


def test_summary_line_reads_like_a_disclosure(plane: ContextPlane) -> None:
    response = plane.search(
        "u_kim", SearchRequest(question="parental leave", purpose="customer_support")
    )
    summary = response.summary_line()
    assert "withheld" in summary


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #


def _source(source_id: str, description: str) -> Source:
    return Source(
        id=source_id, kind="docs", title=source_id, description=description, owner="o"
    )


def test_router_prefers_the_matching_description() -> None:
    router = SemanticRouter()
    sources = [
        _source("payroll", "salary payments, payslips, tax withholding"),
        _source("runbooks", "incident response, database failover, paging"),
    ]
    routed = router.route("how do I fail over the database", sources, limit=1)
    assert routed[0].source_id == "runbooks"


def test_router_falls_back_rather_than_returning_nothing() -> None:
    """An empty routing result would be indistinguishable from a denial."""
    router = SemanticRouter()
    sources = [_source("a", "alpha"), _source("b", "beta")]
    routed = router.route("zzzz unrelated question", sources, limit=2)
    assert len(routed) == 2
    assert all("no description matched" in choice.reason for choice in routed)


def test_router_returns_nothing_when_no_source_is_eligible() -> None:
    assert SemanticRouter().route("anything", []) == []
