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
    """A client backed by a workspace in a temp directory, isolated per test."""
    monkeypatch.setattr(playground, "_plane", None)
    monkeypatch.setattr(playground.tempfile, "gettempdir", lambda: str(tmp_path))
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
