"""MCP server over stdio.

Implements the Model Context Protocol handshake and tool surface directly on
newline-delimited JSON-RPC 2.0. Hand-rolling it costs about two hundred lines and
buys a dependency-free install, which matters when the thing being deployed is a
security control inside a locked-down network.

Identity model: a server instance acts as exactly one principal, configured at
launch. The agent cannot choose who it is, because anything an agent can state in a
tool argument is something a prompt injection can also state. ``--allow-principal-
override`` exists for local development and says so loudly.

The text returned to the model is deliberately shaped: withheld records are stated
first and the model is told to disclose them. A guardrail the model can silently
ignore is not a guardrail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from .plane import ContextPlane
from .types import ResultRecord, SearchRequest, WithheldGroup
from .workspace import Workspace

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aperture"

# JSON-RPC error codes used here.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


TOOLS: list[dict[str, Any]] = [
    {
        "name": "context_search",
        "description": (
            "Search governed enterprise context. Returns records the caller is "
            "permitted to see, with provenance, plus an explicit account of anything "
            "withheld. If records were withheld, say so in your answer instead of "
            "answering as though the context were complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The natural-language question to answer.",
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        "Why this data is being accessed, e.g. customer_support. "
                        "Access differs by purpose; declare the real one."
                    ),
                },
                "max_records": {"type": "integer", "default": 8},
            },
            "required": ["question"],
        },
    },
    {
        "name": "context_fetch",
        "description": (
            "Fetch one record by id from a registered source. Permissions are "
            "re-evaluated on every fetch; a record id from an earlier result is not "
            "a durable capability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "record_id": {"type": "string"},
                "purpose": {"type": "string"},
            },
            "required": ["source_id", "record_id"],
        },
    },
    {
        "name": "catalog_list_sources",
        "description": (
            "List the data sources this caller may read under a given purpose, with "
            "what each one covers. Use it to decide whether the question can be "
            "answered from governed context at all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"purpose": {"type": "string"}},
        },
    },
    {
        "name": "access_explain",
        "description": (
            "Return the audit record for an earlier trace_id: identity, purpose, "
            "sources consulted, records returned, and everything withheld with reasons."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"trace_id": {"type": "string"}},
            "required": ["trace_id"],
        },
    },
]


def _render_records(records: list[ResultRecord]) -> list[dict[str, Any]]:
    """Shape records for the model: content plus the provenance to cite."""
    return [
        {
            "record_id": record.id,
            "title": record.title,
            "text": record.text,
            "source": record.citation.source_id,
            "source_title": record.citation.source_title,
            "owner": record.citation.owner,
            "sensitivity": str(record.citation.sensitivity),
            "age_days": record.citation.age_days,
            "redacted_fields": list(record.redacted_fields),
            "notes": list(record.notes),
        }
        for record in records
    ]


def _render_withheld(groups: list[WithheldGroup]) -> list[dict[str, Any]]:
    return [
        {
            "reason": str(group.reason),
            "explanation": group.explanation,
            "count": group.count,
            "sources": list(group.sources),
        }
        for group in groups
    ]


class ApertureMCPServer:
    """Serves the plane's read tools to an MCP client."""

    def __init__(
        self,
        plane: ContextPlane,
        principal_id: str,
        default_purpose: str | None = None,
        allow_principal_override: bool = False,
    ) -> None:
        self.plane = plane
        self.principal_id = principal_id
        self.default_purpose = default_purpose
        self.allow_principal_override = allow_principal_override
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "ping": lambda _params: {},
        }

    # -- identity --------------------------------------------------------- #

    def _resolve_principal(self, arguments: dict[str, Any]) -> str:
        """Return the acting principal, honoring override only when enabled."""
        if self.allow_principal_override:
            return str(arguments.get("principal") or self.principal_id)
        return self.principal_id

    def _resolve_purpose(self, arguments: dict[str, Any]) -> str:
        purpose = arguments.get("purpose") or self.default_purpose
        if not purpose:
            raise ValueError(
                "no purpose declared and this server has no default purpose configured"
            )
        return str(purpose)

    # -- protocol --------------------------------------------------------- #

    def _initialize(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
            "instructions": (
                "All enterprise data reaches you through Aperture. Every result "
                "carries provenance and, when applicable, a list of records that were "
                "withheld. Always disclose withheld records to the user rather than "
                "answering as if the context were complete."
            ),
        }

    def _tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": TOOLS}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            payload = self._dispatch_tool(str(name), arguments)
        except ValueError as exc:
            return {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(payload, indent=2, default=str)}
            ],
            "isError": False,
        }

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        principal = self._resolve_principal(arguments)

        if name == "context_search":
            question = arguments.get("question")
            if not question:
                raise ValueError("question is required")
            response = self.plane.search(
                principal,
                SearchRequest(
                    question=str(question),
                    purpose=self._resolve_purpose(arguments),
                    max_records=int(arguments.get("max_records", 8)),
                ),
            )
            payload: dict[str, Any] = {
                "trace_id": response.trace_id,
                "summary": response.summary_line(),
                "records": _render_records(response.records),
                "sources_consulted": response.sources_consulted,
            }
            if response.withheld:
                payload["withheld"] = _render_withheld(response.withheld)
                payload["disclosure_required"] = (
                    "Some records were withheld. Tell the user your answer may be "
                    "incomplete and state the reasons above."
                )
            return payload

        if name == "context_fetch":
            source_id = arguments.get("source_id")
            record_id = arguments.get("record_id")
            if not source_id or not record_id:
                raise ValueError("source_id and record_id are required")
            result = self.plane.fetch(
                principal,
                str(source_id),
                str(record_id),
                self._resolve_purpose(arguments),
            )
            if isinstance(result, ResultRecord):
                return {"record": _render_records([result])[0]}
            return {"withheld": _render_withheld([result])[0]}

        if name == "catalog_list_sources":
            return {"sources": self.plane.list_sources(principal, self._resolve_purpose(arguments))}

        if name == "access_explain":
            trace_id = arguments.get("trace_id")
            if not trace_id:
                raise ValueError("trace_id is required")
            entry = self.plane.explain(str(trace_id))
            if entry is None:
                raise ValueError(f"no audit record for trace {trace_id}")
            return {"audit_record": entry}

        raise ValueError(f"unknown tool: {name}")

    # -- transport -------------------------------------------------------- #

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC message, returning a response or None for notifications."""
        method = str(message.get("method", ""))
        message_id = message.get("id")

        if message_id is None:  # notification
            return None

        handler = self.handlers.get(method)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": METHOD_NOT_FOUND, "message": f"unknown method: {method}"},
            }

        try:
            result = handler(message.get("params") or {})
        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the server
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": INTERNAL_ERROR, "message": str(exc)},
            }
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def run(self, stdin: TextIO, stdout: TextIO) -> None:
        """Serve until stdin closes."""
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, default=str) + "\n")
                stdout.flush()


def serve_stdio(
    workspace_root: Path,
    principal_id: str,
    default_purpose: str | None = None,
    allow_principal_override: bool = False,
) -> None:
    """Load a workspace and serve it over stdio.

    Configuration errors surface before the first request: an invalid policy stops
    the server rather than starting one that cannot enforce it.
    """
    plane = ContextPlane(Workspace.load(workspace_root))
    if plane.workspace.principals.get(principal_id) is None:
        raise SystemExit(
            f"principal '{principal_id}' is not registered in this workspace"
        )
    server = ApertureMCPServer(
        plane,
        principal_id=principal_id,
        default_purpose=default_purpose,
        allow_principal_override=allow_principal_override,
    )
    server.run(sys.stdin, sys.stdout)
