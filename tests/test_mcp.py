"""MCP protocol contract.

The transport is hand-rolled, so these tests pin the wire behavior an MCP client
depends on: handshake shape, tool discovery, call results, notification handling,
and error responses that do not take the server down.
"""

from __future__ import annotations

import io
import json

from aperture.mcp_server import PROTOCOL_VERSION, ApertureMCPServer
from aperture.plane import ContextPlane


def make_server(plane: ContextPlane, **kwargs) -> ApertureMCPServer:
    options = {"principal_id": "u_kim", "default_purpose": "customer_support"}
    options.update(kwargs)
    return ApertureMCPServer(plane, **options)


def request(server: ApertureMCPServer, method: str, params: dict | None = None, id_: int = 1):
    return server.handle({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def tool_payload(response) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_initialize_reports_protocol_and_tool_capability(plane: ContextPlane) -> None:
    result = request(make_server(plane), "initialize")["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "aperture"


def test_initialize_instructions_require_disclosure(plane: ContextPlane) -> None:
    """The client-visible instructions must tell the model to surface withheld records."""
    result = request(make_server(plane), "initialize")["result"]
    assert "withheld" in result["instructions"]


def test_tools_list_exposes_the_read_surface(plane: ContextPlane) -> None:
    tools = request(make_server(plane), "tools/list")["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {"context_search", "context_fetch", "catalog_list_sources", "access_explain"}
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_notifications_get_no_response(plane: ContextPlane) -> None:
    assert make_server(plane).handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_returns_an_error_not_a_crash(plane: ContextPlane) -> None:
    response = request(make_server(plane), "resources/list")
    assert response["error"]["code"] == -32601


def test_search_returns_records_and_trace(plane: ContextPlane) -> None:
    response = request(
        make_server(plane),
        "tools/call",
        {"name": "context_search", "arguments": {"question": "refund window"}},
    )
    payload = tool_payload(response)
    assert payload["records"]
    assert payload["trace_id"].startswith("trc_")
    assert payload["sources_consulted"] == ["support_kb"]


def test_search_attaches_a_disclosure_requirement_when_records_are_withheld(
    plane: ContextPlane,
) -> None:
    response = request(
        make_server(plane),
        "tools/call",
        {"name": "context_search", "arguments": {"question": "parental leave"}},
    )
    payload = tool_payload(response)
    assert payload["withheld"]
    assert "incomplete" in payload["disclosure_required"]


def test_missing_purpose_is_a_tool_error(plane: ContextPlane) -> None:
    server = make_server(plane, default_purpose=None)
    response = request(
        server, "tools/call", {"name": "context_search", "arguments": {"question": "anything"}}
    )
    assert response["result"]["isError"] is True
    assert "purpose" in tool_payload(response)["error"]


def test_unknown_tool_is_reported_as_a_tool_error(plane: ContextPlane) -> None:
    response = request(make_server(plane), "tools/call", {"name": "context_delete", "arguments": {}})
    assert response["result"]["isError"] is True


def test_fetch_returns_the_withheld_reason_instead_of_content(plane: ContextPlane) -> None:
    response = request(
        make_server(plane),
        "tools/call",
        {
            "name": "context_fetch",
            "arguments": {"source_id": "support_kb", "record_id": "kb-globex-onboarding"},
        },
    )
    payload = tool_payload(response)
    assert payload["withheld"]["reason"] == "tenant_mismatch"


def test_access_explain_returns_the_audit_record(plane: ContextPlane) -> None:
    server = make_server(plane)
    search = tool_payload(
        request(
            server, "tools/call", {"name": "context_search", "arguments": {"question": "plan limits"}}
        )
    )
    explained = tool_payload(
        request(
            server,
            "tools/call",
            {"name": "access_explain", "arguments": {"trace_id": search["trace_id"]}},
        )
    )
    assert explained["audit_record"]["principal_id"] == "u_kim"


def test_catalog_listing_reflects_the_caller_identity(plane: ContextPlane) -> None:
    payload = tool_payload(
        request(make_server(plane), "tools/call", {"name": "catalog_list_sources", "arguments": {}})
    )
    assert [source["id"] for source in payload["sources"]] == ["support_kb"]


def test_stdio_loop_reads_and_writes_newline_delimited_json(plane: ContextPlane) -> None:
    server = make_server(plane)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + "not json at all\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        + "\n"
    )
    stdout = io.StringIO()
    server.run(stdin, stdout)

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [message["id"] for message in lines] == [1, 2]


# --------------------------------------------------------------------------- #
# action tools
# --------------------------------------------------------------------------- #


def make_action_server(plane: ContextPlane) -> ApertureMCPServer:
    return ApertureMCPServer(
        plane,
        principal_id="svc_support_agent",
        default_purpose="customer_support",
        gateway=plane.workspace.gateway(),
    )


def test_action_tools_are_hidden_in_a_read_only_workspace(plane: ContextPlane) -> None:
    """An agent should not be shown verbs this deployment never grants."""
    tools = request(make_server(plane), "tools/list")["result"]["tools"]
    assert not any(tool["name"].startswith("action_") for tool in tools)


def test_action_tools_appear_when_actions_are_registered(plane: ContextPlane) -> None:
    tools = request(make_action_server(plane), "tools/list")["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"action_list", "action_propose", "action_execute", "action_status"} <= names


def test_propose_returns_the_blast_radius_and_a_next_step(plane: ContextPlane) -> None:
    payload = tool_payload(
        request(
            make_action_server(plane),
            "tools/call",
            {
                "name": "action_propose",
                "arguments": {
                    "action_id": "support.refund",
                    "arguments": {"customer_id": "cus-5510", "amount": 40},
                },
            },
        )
    )
    assert payload["state"] == "ready"
    assert payload["blast_radius"]["amount"] == 40
    assert payload["blast_radius"]["reversible"] is True
    assert "action_execute" in payload["next_step"]


def test_proposal_needing_approval_tells_the_model_not_to_retry(plane: ContextPlane) -> None:
    payload = tool_payload(
        request(
            make_action_server(plane),
            "tools/call",
            {
                "name": "action_propose",
                "arguments": {
                    "action_id": "support.message_customer",
                    "arguments": {"to": "a@b.example", "subject": "hi", "body": "hello"},
                },
            },
        )
    )
    assert payload["state"] == "pending_approval"
    assert payload["blast_radius"]["reversible"] is False
    assert "do not retry" in payload["next_step"]


def test_refusals_come_back_as_data_with_a_reason(plane: ContextPlane) -> None:
    """A refusal is an outcome the model must relay, not a protocol error."""
    payload = tool_payload(
        request(
            make_action_server(plane),
            "tools/call",
            {
                "name": "action_propose",
                "arguments": {
                    "action_id": "support.refund",
                    "arguments": {"customer_id": "cus-4471", "amount": 9000},
                },
            },
        )
    )
    assert payload["refused"] is True
    assert payload["reason"] == "impact_limit_exceeded"
    assert "did not happen" in payload["disclosure_required"]


def test_execute_reports_whether_the_result_can_be_undone(plane: ContextPlane) -> None:
    server = make_action_server(plane)
    proposal = tool_payload(
        request(
            server,
            "tools/call",
            {
                "name": "action_propose",
                "arguments": {
                    "action_id": "support.close_ticket",
                    "arguments": {"ticket_id": "tkt-1181"},
                },
            },
        )
    )
    executed = tool_payload(
        request(
            server,
            "tools/call",
            {"name": "action_execute", "arguments": {"proposal_id": proposal["proposal_id"]}},
        )
    )
    assert executed["executed"] is True
    assert executed["reversible"] is True
