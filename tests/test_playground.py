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
