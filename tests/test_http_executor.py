"""HTTP executor, exercised against a real local server.

Mocking the transport would prove nothing about the transport. These tests run an
actual HTTP server in a thread, so timeouts, redirects, error codes, and headers
behave the way they will in production.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from aperture.actions.executors import ExecutorError
from aperture.actions.http_executor import HttpExecutor
from aperture.actions.types import ActionSpec

RECEIVED: list[dict] = []


class StubHandler(BaseHTTPRequestHandler):
    """A stand-in for Stripe, Zendesk, or an internal operations API."""

    def log_message(self, *args):  # noqa: D102 - keep test output quiet
        return

    def _respond(self, status: int, payload: dict | str) -> None:
        body = (json.dumps(payload) if isinstance(payload, dict) else payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        RECEIVED.append({"path": self.path, "payload": payload, "headers": dict(self.headers)})

        if self.path == "/estimate":
            self._respond(200, {
                "summary": "Refund 250.00 USD to Rivera Logistics",
                "affected": 1, "amount": 250.0, "external_recipients": ["ops@rivera.example"],
            })
        elif self.path == "/execute":
            self._respond(200, {
                "result": {"refund_id": "re_123"}, "compensation": {"refund_id": "re_123"},
            })
        elif self.path == "/execute-no-compensation":
            self._respond(200, {"result": {"refund_id": "re_456"}})
        elif self.path == "/compensate":
            self._respond(200, {"result": {"refund_id": "re_123", "status": "reversed"}})
        elif self.path == "/boom":
            self._respond(500, {"error": "provider unavailable"})
        elif self.path == "/garbage":
            self._respond(200, "not json at all")
        elif self.path == "/no-summary":
            self._respond(200, {"affected": 3})
        elif self.path == "/slow":
            time.sleep(2.0)
            self._respond(200, {"summary": "eventually"})
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        else:
            self._respond(404, {"error": "no such endpoint"})


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def clear_received():
    RECEIVED.clear()


def spec(base: str, **overrides) -> ActionSpec:
    config = {
        "estimate_url": f"{base}/estimate",
        "execute_url": f"{base}/execute",
        "compensate_url": f"{base}/compensate",
    }
    config.update(overrides)
    return ActionSpec(
        id="ops.refund",
        title="Refund",
        description="Refund via the operations API",
        executor="http",
        owner="ops@acme.example",
        effect_class="financial",
        reversible=True,
        config=config,
    )


@pytest.fixture()
def executor(tmp_path: Path) -> HttpExecutor:
    return HttpExecutor(tmp_path)


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_estimate_maps_the_response_to_a_blast_radius(executor, server) -> None:
    blast = executor.estimate(spec(server), {"customer_id": "cus-1", "amount": 250})
    assert blast.amount == 250.0
    assert blast.affected == 1
    assert blast.external_recipients == ("ops@rivera.example",)
    assert blast.reversible is True
    assert "Rivera" in blast.summary


def test_estimate_declares_itself_a_dry_run(executor, server) -> None:
    """The contract has to say so, or a remote service may act on an estimate."""
    executor.estimate(spec(server), {"amount": 10})
    assert RECEIVED[0]["payload"]["dry_run"] is True


def test_execute_returns_result_and_compensation(executor, server) -> None:
    result, compensation = executor.execute(spec(server), {"amount": 250})
    assert result == {"refund_id": "re_123"}
    assert compensation == {"refund_id": "re_123"}


def test_compensate_calls_the_undo_endpoint(executor, server) -> None:
    result = executor.compensate(spec(server), {"refund_id": "re_123"})
    assert result["status"] == "reversed"
    assert RECEIVED[0]["path"] == "/compensate"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_credentials_come_from_the_environment(
    executor, server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog names the variable; the secret itself never lands in git."""
    monkeypatch.setenv("OPS_API_TOKEN", "Bearer s3cret")
    executor.estimate(
        spec(server, auth_env="OPS_API_TOKEN", auth_header="Authorization"), {"amount": 1}
    )
    assert RECEIVED[0]["headers"]["Authorization"] == "Bearer s3cret"


def test_missing_credential_is_a_clear_failure(executor, server) -> None:
    with pytest.raises(ExecutorError, match="OPS_API_TOKEN"):
        executor.estimate(spec(server, auth_env="OPS_API_TOKEN"), {"amount": 1})


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #


def test_redirects_are_refused(executor, server) -> None:
    """Following a redirect turns one allowlisted URL into an arbitrary one.

    The stub redirects at the cloud metadata endpoint, which is the classic SSRF
    target for stealing instance credentials.
    """
    with pytest.raises(ExecutorError, match="redirect"):
        executor.estimate(spec(server, estimate_url=f"{server}/redirect"), {"amount": 1})


def test_plaintext_http_to_a_remote_host_is_refused(executor) -> None:
    """Action credentials must not cross a network in the clear."""
    with pytest.raises(ExecutorError, match="plaintext"):
        executor.estimate(spec("http://ops.example.com"), {"amount": 1})


def test_localhost_over_plain_http_is_allowed(executor, server) -> None:
    """Local development would be impossible otherwise, and nothing leaves the host."""
    assert executor.estimate(spec(server), {"amount": 1}).amount == 250.0


def test_non_http_schemes_are_refused(executor) -> None:
    with pytest.raises(ExecutorError, match="http"):
        executor.estimate(spec("file:///etc"), {"amount": 1})


def test_timeouts_are_enforced(executor, server) -> None:
    """A hung endpoint must not hold an execution claim open indefinitely."""
    with pytest.raises(ExecutorError, match="unreachable|timed out|time"):
        executor.estimate(
            spec(server, estimate_url=f"{server}/slow", timeout_seconds=0.3), {"amount": 1}
        )


# --------------------------------------------------------------------------- #
# bad responses
# --------------------------------------------------------------------------- #


def test_error_status_is_reported_with_detail(executor, server) -> None:
    with pytest.raises(ExecutorError, match="500"):
        executor.estimate(spec(server, estimate_url=f"{server}/boom"), {"amount": 1})


def test_invalid_json_is_reported(executor, server) -> None:
    with pytest.raises(ExecutorError, match="invalid JSON"):
        executor.estimate(spec(server, estimate_url=f"{server}/garbage"), {"amount": 1})


def test_estimate_without_a_summary_is_refused(executor, server) -> None:
    """A blast radius with no description is useless to a human reviewer."""
    with pytest.raises(ExecutorError, match="summary"):
        executor.estimate(spec(server, estimate_url=f"{server}/no-summary"), {"amount": 1})


def test_reversible_action_that_returns_no_compensation_fails_loudly(executor, server) -> None:
    """The approval screen said this could be undone. It has to be true.

    Silently accepting the execution would leave a record that cannot be rolled
    back despite the catalog and the reviewer both believing it can.
    """
    with pytest.raises(ExecutorError, match="no compensation"):
        executor.execute(
            spec(server, execute_url=f"{server}/execute-no-compensation"), {"amount": 1}
        )


def test_irreversible_action_may_return_no_compensation(executor, server) -> None:
    irreversible = spec(server, execute_url=f"{server}/execute-no-compensation").model_copy(
        update={"reversible": False}
    )
    result, compensation = executor.execute(irreversible, {"amount": 1})
    assert result == {"refund_id": "re_456"}
    assert compensation is None


def test_unconfigured_endpoint_is_refused(executor, server) -> None:
    bare = spec(server).model_copy(update={"config": {"execute_url": f"{server}/execute"}})
    with pytest.raises(ExecutorError, match="estimate_url"):
        executor.estimate(bare, {"amount": 1})
