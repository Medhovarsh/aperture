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

from .actions.gateway import ActionGateway
from .actions.types import ActionRefusal, Proposal
from .assertions import AssertionFailure, AssertionVerifier
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
    {
        "name": "action_list",
        "description": (
            "List the actions this caller may take under a purpose, with whether each "
            "one can be undone and whether it needs human approval. Check here before "
            "promising a user that something can be done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"purpose": {"type": "string"}},
        },
    },
    {
        "name": "action_propose",
        "description": (
            "Propose an action. This does NOT perform it. The gateway measures the "
            "real blast radius, applies policy, and returns either a proposal you may "
            "execute, a proposal awaiting human approval, or a refusal with a reason. "
            "Always show the blast radius to the user before executing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string", "description": "e.g. support.refund"},
                "arguments": {"type": "object", "description": "Action arguments"},
                "purpose": {"type": "string"},
            },
            "required": ["action_id", "arguments"],
        },
    },
    {
        "name": "action_execute",
        "description": (
            "Execute a proposal that is in the 'ready' state. Proposals awaiting "
            "approval cannot be executed until a human approves them out of band; "
            "poll action_status rather than retrying."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}},
            "required": ["proposal_id"],
        },
    },
    {
        "name": "action_status",
        "description": (
            "Check a proposal: its state, blast radius, and any human decision. Use "
            "this to tell the user whether their request is waiting on an approver."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}},
            "required": ["proposal_id"],
        },
    },
]

# Approve and deny are deliberately absent from the tool surface. An agent that could
# approve its own proposal would make the approval step decorative, and an agent that
# could approve another agent's would make it worse. Humans decide out of band, through
# the CLI or a review UI.


def with_assertion_argument(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the assertion argument to every tool schema.

    Used when the server runs in signed-identity mode, so a client discovers that
    it must present a token rather than finding out through a refusal.
    """
    extended: list[dict[str, Any]] = []
    for tool in tools:
        schema = json.loads(json.dumps(tool["inputSchema"]))
        schema.setdefault("properties", {})["assertion"] = {
            "type": "string",
            "description": (
                "Signed caller assertion proving who is acting and for what purpose. "
                "Single use; expires quickly."
            ),
        }
        schema["required"] = sorted(set(schema.get("required", [])) | {"assertion"})
        extended.append({**tool, "inputSchema": schema})
    return extended


def _render_action_result(result: "Proposal | ActionRefusal") -> dict[str, Any]:
    """Shape a proposal or refusal for the model, with the next step spelled out."""
    if isinstance(result, ActionRefusal):
        return {
            "refused": True,
            "reason": str(result.reason),
            "explanation": result.explanation,
            "detail": result.detail,
            "disclosure_required": (
                "This action was refused. Tell the user it did not happen and why."
            ),
        }

    payload: dict[str, Any] = {
        "proposal_id": result.id,
        "state": str(result.state),
        "action_id": result.action_id,
        "arguments": result.arguments,
        "blast_radius": {
            "summary": result.blast.summary,
            "headline": result.blast.headline(),
            "affected": result.blast.affected,
            "amount": result.blast.amount,
            "currency": result.blast.currency,
            "external_recipients": list(result.blast.external_recipients),
            "reversible": result.blast.reversible,
        },
        "matched_rules": list(result.matched_rules),
    }
    if result.state == "pending_approval":
        payload["next_step"] = (
            "A human must approve this before it can run. Tell the user the action is "
            "pending approval and show them the blast radius; do not retry execution."
        )
    elif result.state == "ready":
        payload["next_step"] = (
            "Show the blast radius to the user, then call action_execute with this "
            "proposal_id if they confirm."
        )
    if result.approval:
        payload["approval"] = {
            "approved": result.approval.approved,
            "decided_by": result.approval.decided_by,
            "note": result.approval.note,
        }
    return payload


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
        gateway: ActionGateway | None = None,
        verifier: AssertionVerifier | None = None,
    ) -> None:
        self.plane = plane
        self.gateway = gateway
        # When a verifier is configured the server serves many identities, each
        # proved per call by a signed assertion, instead of one fixed principal.
        self.verifier = verifier
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

    def _resolve_identity(self, arguments: dict[str, Any]) -> tuple[str, str]:
        """Return the acting (principal, purpose) for one tool call.

        Three modes, in descending order of trustworthiness:

        1. **Signed assertion.** A trusted issuer vouched for this identity and
           purpose together, and the token is single use. The caller cannot widen
           either one, because both are covered by the signature.
        2. **Server-pinned identity.** One process serves one principal. The agent
           cannot choose.
        3. **Caller-asserted identity.** Local development only, and the flag that
           enables it says so.
        """
        if self.verifier is not None:
            token = arguments.get("assertion")
            if not token:
                raise ValueError(
                    "this server requires a signed caller assertion; pass 'assertion'"
                )
            result = self.verifier.verify(str(token))
            if isinstance(result, AssertionFailure):
                raise ValueError(f"{result.reason}: {result.detail}")
            return result.principal_id, result.purpose

        principal = (
            str(arguments.get("principal") or self.principal_id)
            if self.allow_principal_override
            else self.principal_id
        )
        purpose = arguments.get("purpose") or self.default_purpose
        if not purpose:
            raise ValueError(
                "no purpose declared and this server has no default purpose configured"
            )
        return principal, str(purpose)

    def _resolve_purpose(self, arguments: dict[str, Any]) -> str:
        """Purpose for one call, after identity has already been resolved.

        Deliberately does no verification. An assertion is single use, so it is
        checked exactly once per call in :meth:`_dispatch_tool`, which then injects
        the resolved purpose into the arguments this reads. Verifying again here
        would make every signed call fail as its own replay.
        """
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
        """Advertise the read tools, plus action tools when actions are registered.

        A read-only workspace should not show an agent verbs it can never use.
        """
        tools = TOOLS
        if self.gateway is None or not len(self.gateway.catalog):
            tools = [tool for tool in tools if not tool["name"].startswith("action_")]
        if self.verifier is not None:
            tools = with_assertion_argument(tools)
        return {"tools": tools}

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
        # Resolved once per call: a single-use assertion must not be verified twice.
        principal, purpose = self._resolve_identity(arguments)
        arguments = {**arguments, "purpose": purpose}

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

        if name.startswith("action_"):
            return self._dispatch_action(name, arguments, principal)

        raise ValueError(f"unknown tool: {name}")

    def _dispatch_action(
        self, name: str, arguments: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        """Handle the action tools.

        Refusals come back as data with a reason code rather than as protocol errors,
        because a refusal is a legitimate outcome the model must relay to the user.
        """
        if self.gateway is None:
            raise ValueError("this workspace registers no actions")

        if name == "action_list":
            return {"actions": self.gateway.list_actions(principal, self._resolve_purpose(arguments))}

        if name == "action_propose":
            action_id = arguments.get("action_id")
            if not action_id:
                raise ValueError("action_id is required")
            result = self.gateway.propose(
                principal,
                self._resolve_purpose(arguments),
                str(action_id),
                dict(arguments.get("arguments") or {}),
            )
            return _render_action_result(result)

        if name == "action_execute":
            proposal_id = arguments.get("proposal_id")
            if not proposal_id:
                raise ValueError("proposal_id is required")
            result = self.gateway.execute(str(proposal_id), principal)
            if isinstance(result, ActionRefusal):
                return _render_action_result(result)
            return {
                "executed": True,
                "execution_id": result.id,
                "action_id": result.action_id,
                "result": result.result,
                "reversible": result.reversible,
                "undo": (
                    "A human can undo this through the Aperture CLI."
                    if result.reversible
                    else "This action cannot be undone."
                ),
            }

        if name == "action_status":
            proposal_id = arguments.get("proposal_id")
            if not proposal_id:
                raise ValueError("proposal_id is required")
            proposal = self.gateway.store.get_proposal(str(proposal_id))
            if proposal is None:
                raise ValueError(f"no such proposal: {proposal_id}")
            return _render_action_result(proposal)

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
    signing_secret: str | None = None,
) -> None:
    """Load a workspace and serve it over stdio.

    Configuration errors surface before the first request: an invalid policy stops
    the server rather than starting one that cannot enforce it.
    """
    workspace = Workspace.load(workspace_root)
    plane = ContextPlane(workspace)
    verifier = (
        AssertionVerifier(signing_secret, nonce_store=workspace.action_store)
        if signing_secret
        else None
    )
    if verifier is None and workspace.principals.get(principal_id) is None:
        raise SystemExit(
            f"principal '{principal_id}' is not registered in this workspace"
        )
    server = ApertureMCPServer(
        plane,
        principal_id=principal_id,
        default_purpose=default_purpose,
        allow_principal_override=allow_principal_override,
        gateway=workspace.gateway() if len(workspace.actions) else None,
        verifier=verifier,
    )
    server.run(sys.stdin, sys.stdout)
