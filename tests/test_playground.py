"""Playground HTTP surface.

The playground is a demo, but it is also the thing most people will see first, so
its behavior is pinned: it must show the same purpose-binding outcome the CLI and
the MCP server show, and it must never quietly drop the withheld list.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="playground needs the [web] extra")

from fastapi.testclient import TestClient  # noqa: E402

from aperture import playground  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client with its own session pool rooted in a temp directory."""
    monkeypatch.setattr(playground, "sessions", playground.SessionPool(tmp_path / "sessions"))
    # A frozen clock keeps window-boundary assertions deterministic; a real clock
    # makes them fail whenever the suite runs slowly.
    monkeypatch.setattr(playground, "limiter", playground.RateLimiter(clock=lambda: 1000.0))
    return TestClient(playground.app)


def ask(client: TestClient, principal: str, purpose: str, question: str) -> dict:
    response = client.post(
        "/api/search",
        json={"principal_id": principal, "purpose": purpose, "question": question},
    )
    assert response.status_code == 200
    return response.json()


def test_index_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Aperture" in response.text
    assert "text/html" in response.headers["content-type"]


def test_index_has_no_external_asset_references(client: TestClient) -> None:
    """The page must work with no network access beyond its own origin."""
    body = client.get("/").text
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "cdn." not in body


def test_identities_lists_workspace_configuration(client: TestClient) -> None:
    data = client.get("/api/identities").json()
    assert {p["id"] for p in data["principals"]} == {
        "u_dana", "u_raj", "u_kim", "svc_support_agent", "u_ops", "u_sam", "u_partner"
    }
    assert "hr_support" in data["purposes"]


def test_purpose_binding_is_visible_through_http(client: TestClient) -> None:
    """The headline demo must hold on the hosted surface."""
    question = "how much parental leave do we offer"
    hr = ask(client, "u_dana", "hr_support", question)
    support = ask(client, "u_kim", "customer_support", question)

    assert [r["source_id"] for r in hr["records"]] == ["hr_handbook"]
    assert all(r["source_id"] != "hr_handbook" for r in support["records"])
    assert support["withheld"]


def test_redaction_is_surfaced_to_the_ui(client: TestClient) -> None:
    data = ask(client, "u_dana", "hr_support", "Raj Mehta platform engineer manager location")
    record = next(r for r in data["records"] if r["source_id"] == "people_db")
    assert set(record["redacted_fields"]) == {"salary", "national_id"}
    assert "212000" not in record["text"]


def test_withheld_reasons_are_always_returned(client: TestClient) -> None:
    data = ask(client, "u_partner", "customer_support", "acme internal policies")
    assert data["records"] == []
    assert data["withheld"], "a denial with no explanation is the bug this product exists to fix"


def test_overlong_question_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/search",
        json={"principal_id": "u_kim", "purpose": "customer_support", "question": "x" * 5000},
    )
    assert response.status_code == 422


def test_lineage_endpoint_reports_an_intact_chain(client: TestClient) -> None:
    ask(client, "u_kim", "customer_support", "refund window")
    data = client.get("/api/lineage").json()
    assert data["chain_intact"] is True
    assert data["entries"]
    assert data["entries"][-1]["principal_id"] == "u_kim"


def test_lineage_records_denials_too(client: TestClient) -> None:
    ask(client, "u_partner", "hr_support", "salaries")
    entries = client.get("/api/lineage").json()["entries"]
    assert entries[-1]["returned"] == 0
    assert entries[-1]["withheld"]


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #


def propose(client: TestClient, **kwargs) -> dict:
    payload = {
        "principal_id": "svc_support_agent",
        "purpose": "customer_support",
        "action_id": "support.refund",
        "arguments": {"customer_id": "cus-4471", "amount": 3000},
    }
    payload.update(kwargs)
    response = client.post("/api/actions/propose", json=payload)
    assert response.status_code == 200
    return response.json()


def test_action_listing_follows_the_selected_identity(client: TestClient) -> None:
    support = client.get(
        "/api/actions",
        params={"principal_id": "svc_support_agent", "purpose": "customer_support"},
    ).json()["actions"]
    assert {a["id"] for a in support} == {
        "support.refund", "support.close_ticket", "support.message_customer"
    }

    partner = client.get(
        "/api/actions", params={"principal_id": "u_partner", "purpose": "customer_support"}
    ).json()["actions"]
    assert partner == []


def test_propose_returns_a_measured_blast_radius(client: TestClient) -> None:
    data = propose(client)
    assert data["refused"] is False
    assert data["state"] == "pending_approval"
    assert data["blast"]["amount"] == 3000
    assert data["blast"]["reversible"] is True


def test_over_limit_proposal_is_refused_with_a_reason(client: TestClient) -> None:
    data = propose(client, arguments={"customer_id": "cus-4471", "amount": 9000})
    assert data["refused"] is True
    assert data["reason"] == "impact_limit_exceeded"


def test_purge_shows_the_gap_between_argument_and_consequence(client: TestClient) -> None:
    data = propose(
        client,
        principal_id="u_ops",
        purpose="data_retention",
        action_id="ops.purge_region",
        arguments={"region": "legacy"},
    )
    assert data["refused"] is True
    assert "7 record(s)" in data["detail"]


def test_full_approve_execute_rollback_loop(client: TestClient) -> None:
    proposal = propose(client)

    decided = client.post(
        "/api/actions/decide",
        json={"proposal_id": proposal["proposal_id"], "approver_id": "u_kim", "approved": True},
    ).json()
    assert decided["state"] == "ready"
    assert decided["approval"]["decided_by"] == "u_kim"

    executed = client.post(
        "/api/actions/execute",
        json={
            "proposal_id": proposal["proposal_id"],
            "principal_id": "svc_support_agent",
        },
    ).json()
    assert executed["executed"] is True
    assert executed["reversible"] is True

    state = client.get("/api/state").json()
    assert [row["status"] for row in state["refunds"]] == ["issued"]

    rolled = client.post(
        "/api/actions/rollback",
        json={"execution_id": executed["execution_id"], "principal_id": "u_kim"},
    ).json()
    assert rolled["rolled_back"] is True
    assert client.get("/api/state").json()["refunds"][0]["status"] == "reversed"


def test_self_approval_is_refused_through_http(client: TestClient) -> None:
    proposal = propose(client)
    decided = client.post(
        "/api/actions/decide",
        json={
            "proposal_id": proposal["proposal_id"],
            "approver_id": "svc_support_agent",
            "approved": True,
        },
    ).json()
    assert decided["refused"] is True
    assert decided["reason"] == "self_approval_forbidden"


def test_lineage_covers_reads_and_actions_in_one_chain(client: TestClient) -> None:
    ask(client, "u_kim", "customer_support", "refund window")
    propose(client)
    data = client.get("/api/lineage").json()
    kinds = {entry["kind"] for entry in data["entries"]}
    assert {"search", "action_proposed"} <= kinds
    assert data["chain_intact"] is True


def test_operations_state_is_exposed_so_effects_are_visible(client: TestClient) -> None:
    state = client.get("/api/state").json()
    assert {"customers", "tickets", "refunds", "messages"} == set(state)
    assert any(row["region"] == "legacy" for row in state["customers"])


# --------------------------------------------------------------------------- #
# production hardening
# --------------------------------------------------------------------------- #


def test_liveness_and_readiness_answer_different_questions(client: TestClient) -> None:
    assert client.get("/healthz").text == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200
    body = ready.json()
    assert body["ready"] is True
    assert body["sources"] == 4
    assert body["lineage_chain_intact"] is True


def test_security_headers_are_set_on_every_response(client: TestClient) -> None:
    headers = client.get("/").headers
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_page_loads_nothing_the_policy_would_block(client: TestClient) -> None:
    """The CSP forbids external resources; the page must not need any."""
    body = client.get("/").text
    assert "<script src=" not in body
    assert "<link rel=\"stylesheet\"" not in body


def test_visitors_do_not_share_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Actions mutate real state, so one visitor's refund must not reach another.

    A shared workspace would also mean a shared rate limit and a shared audit log.
    """
    monkeypatch.setattr(playground, "sessions", playground.SessionPool(tmp_path / "s"))
    monkeypatch.setattr(playground, "limiter", playground.RateLimiter(clock=lambda: 1000.0))
    first, second = TestClient(playground.app), TestClient(playground.app)

    proposal = first.post(
        "/api/actions/propose",
        json={
            "principal_id": "svc_support_agent",
            "purpose": "customer_support",
            "action_id": "support.refund",
            "arguments": {"customer_id": "cus-5510", "amount": 20},
        },
    ).json()
    first.post(
        "/api/actions/execute",
        json={"proposal_id": proposal["proposal_id"], "principal_id": "svc_support_agent"},
    )

    assert len(first.get("/api/state").json()["refunds"]) == 1
    assert second.get("/api/state").json()["refunds"] == []


def test_session_pool_evicts_rather_than_growing_without_bound(tmp_path) -> None:
    """A crawler must not be able to fill the disk one session at a time."""
    pool = playground.SessionPool(tmp_path / "sessions", capacity=3)
    for index in range(6):
        pool.get(f"session-{index}")
    assert pool.size == 3
    assert not (tmp_path / "sessions" / "session-0").exists()
    assert (tmp_path / "sessions" / "session-5").exists()


def test_action_requests_are_rate_limited(client: TestClient) -> None:
    payload = {
        "principal_id": "svc_support_agent",
        "purpose": "customer_support",
        "action_id": "support.refund",
        "arguments": {"customer_id": "cus-5510", "amount": 1},
    }
    statuses = [
        client.post("/api/actions/propose", json=payload).status_code
        for _ in range(playground.ACTION_LIMIT + 5)
    ]
    assert 429 in statuses
    assert statuses.count(200) == playground.ACTION_LIMIT


def test_rate_limited_response_explains_itself(client: TestClient) -> None:
    payload = {
        "principal_id": "svc_support_agent",
        "purpose": "customer_support",
        "action_id": "support.close_ticket",
        "arguments": {"ticket_id": "tkt-1180"},
    }
    for _ in range(playground.ACTION_LIMIT + 1):
        response = client.post("/api/actions/propose", json=payload)
    assert response.status_code == 429
    body = response.json()
    assert body["reason"] == "rate_limit_exceeded"
    assert "requests" in body["explanation"]


def test_reads_and_actions_have_separate_budgets(client: TestClient) -> None:
    """Exploring the search box must not exhaust the allowance for the half that writes."""
    for _ in range(playground.ACTION_LIMIT + 2):
        client.post(
            "/api/actions/propose",
            json={
                "principal_id": "svc_support_agent",
                "purpose": "customer_support",
                "action_id": "support.refund",
                "arguments": {"customer_id": "cus-5510", "amount": 1},
            },
        )
    assert client.get("/api/identities").status_code == 200


def test_rate_limit_window_rolls_over(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the window, the allowance returns. Verified with a controlled clock."""
    now = {"t": 0.0}
    monkeypatch.setattr(playground, "sessions", playground.SessionPool(tmp_path / "s"))
    monkeypatch.setattr(
        playground, "limiter", playground.RateLimiter(clock=lambda: now["t"])
    )
    client = TestClient(playground.app)
    payload = {
        "principal_id": "svc_support_agent",
        "purpose": "customer_support",
        "action_id": "support.close_ticket",
        "arguments": {"ticket_id": "tkt-1180"},
    }

    for _ in range(playground.ACTION_LIMIT):
        assert client.post("/api/actions/propose", json=payload).status_code == 200
    assert client.post("/api/actions/propose", json=payload).status_code == 429

    now["t"] += playground.ACTION_WINDOW + 1
    assert client.post("/api/actions/propose", json=payload).status_code == 200
