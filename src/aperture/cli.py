"""Command-line control plane.

Covers the operator loop: build a workspace, lint the policy, ask a question as a
given identity, explain what happened, and verify the lineage chain.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .actions.types import ActionRefusal, ExecutionRecord, Proposal
from .demo import build_demo_workspace
from .plane import ContextPlane
from .types import SearchRequest
from .workspace import Workspace, WorkspaceError

DEFAULT_WORKSPACE = Path("workspace")


def _load(path: Path) -> ContextPlane:
    return ContextPlane(Workspace.load(path))


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_demo(args: argparse.Namespace) -> int:
    """Generate the demo workspace."""
    root = build_demo_workspace(args.path)
    workspace = Workspace.load(root)
    print(f"Demo workspace created at {root}")
    print(f"  sources:    {len(workspace.catalog)}")
    print(f"  principals: {len(workspace.principals)}")
    print(f"  rules:      {len(workspace.policy.rules)}")
    print()
    print("Try:")
    print(
        f'  aperture query -w {root} -p u_dana --purpose hr_support '
        f'"how much parental leave do we offer"'
    )
    print(
        f'  aperture query -w {root} -p u_kim --purpose customer_support '
        f'"how much parental leave do we offer"'
    )
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Validate the workspace configuration."""
    try:
        workspace = Workspace.load(args.workspace)
    except WorkspaceError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print("Workspace is valid.")
    print(f"  sources:    {len(workspace.catalog)}  ({', '.join(workspace.catalog.ids())})")
    print(f"  principals: {len(workspace.principals)}  ({', '.join(workspace.principals.ids())})")
    print(f"  actions:    {len(workspace.actions)}  ({', '.join(workspace.actions.ids()) or 'read-only workspace'})")
    print(f"  purposes:   {', '.join(workspace.policy.purposes) or '(any)'}")
    print(f"  rules:      {len(workspace.policy.rules)}")
    print(f"  stale action: {workspace.policy.defaults.stale_action}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    """List the sources a principal may read under a purpose."""
    plane = _load(args.workspace)
    visible = plane.list_sources(args.principal, args.purpose)
    if args.json:
        _print_json(visible)
        return 0
    if not visible:
        print(f"{args.principal} may read no sources under purpose '{args.purpose}'.")
        return 0
    print(f"Sources visible to {args.principal} under purpose '{args.purpose}':")
    for source in visible:
        print(f"  {source['id']:<14} [{source['sensitivity']}] {source['title']}")
        print(f"  {'':<14} owner={source['owner']}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """Run a governed retrieval."""
    plane = _load(args.workspace)
    request = SearchRequest(
        question=args.question,
        purpose=args.purpose,
        max_records=args.limit,
        token_budget=args.budget,
    )
    response = plane.search(args.principal, request)

    if args.json:
        _print_json(response.model_dump())
        return 0

    print(f"trace {response.trace_id}   principal={response.principal_id}   purpose={response.purpose}")
    print(f"routed to: {', '.join(response.sources_consulted) or '(no eligible source)'}")
    print()

    if response.records:
        for index, record in enumerate(response.records, 1):
            citation = record.citation
            age = f"{citation.age_days}d old" if citation.age_days is not None else "no timestamp"
            print(f"[{index}] {record.title}  (score {record.score})")
            print(f"    source={record.source_id}  sensitivity={citation.sensitivity}  {age}")
            if record.redacted_fields:
                print(f"    redacted: {', '.join(record.redacted_fields)}")
            for note in record.notes:
                print(f"    note: {note}")
            snippet = record.text.strip().replace("\n", " ")
            print(f"    {snippet[:220]}{'...' if len(snippet) > 220 else ''}")
            print()
    else:
        print("(no records returned)")
        print()

    if response.withheld:
        print("Withheld:")
        for group in response.withheld:
            where = f"  [{', '.join(group.sources)}]" if group.sources else ""
            print(f"  {group.count:>3} x {group.reason} - {group.explanation}{where}")
        print()

    print(response.summary_line())
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Show the lineage entry for a trace id."""
    plane = _load(args.workspace)
    entry = plane.explain(args.trace_id)
    if entry is None:
        print(f"No lineage entry for {args.trace_id}", file=sys.stderr)
        return 1
    _print_json(entry)
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    """Inspect or verify the access log."""
    workspace = Workspace.load(args.workspace)
    log = workspace.lineage

    secret = os.environ.get(args.secret_env) if getattr(args, "secret_env", None) else None

    if args.action == "verify":
        ok, problems = log.verify()
        entries = len(list(log.read_all()))

        # Recomputation catches edits. Only a signed checkpoint held elsewhere
        # catches truncation or a wholesale rewrite.
        if secret:
            anchored_ok, anchored_problems = log.verify_against_checkpoints(secret)
            ok = ok and anchored_ok
            problems = problems + anchored_problems

        if ok:
            checked = f" against {len(log.read_checkpoints())} checkpoint(s)" if secret else ""
            print(f"Lineage chain intact across {entries} entries{checked}.")
            if not secret and log.read_checkpoints():
                print(
                    "  note: checkpoints exist but were not verified; pass --secret-env",
                    file=sys.stderr,
                )
            return 0
        print(f"LINEAGE CHAIN BROKEN ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    if args.action == "checkpoint":
        if not secret:
            print(f"{args.secret_env} is not set", file=sys.stderr)
            return 1
        entry = log.checkpoint(secret)
        print(f"checkpoint written: seq {entry['seq']}  hash {entry['hash'][:16]}")
        print(f"  {log.checkpoint_path}")
        print("  Ship this somewhere the log's writer cannot reach.")
        return 0

    if args.action == "export":
        # Newline-delimited JSON is what every SIEM ingests without a custom parser.
        for entry in log.read_all():
            if entry.get("seq", 0) > args.since:
                print(json.dumps(entry, default=str))
        return 0

    for entry in log.tail(args.limit):
        withheld = ", ".join(f"{w['count']}x{w['reason']}" for w in entry.get("withheld", []))
        print(
            f"{entry['ts']}  {entry['trace_id']}  {entry['principal_id']:<18} "
            f"{entry['purpose']:<18} returned={len(entry.get('returned', []))}"
            f"{('  withheld=' + withheld) if withheld else ''}"
        )
        print(f"    q: {entry['question']}")
    return 0


def _parse_arguments(spec_parameters: dict, pairs: list[str]) -> dict[str, object]:
    """Turn repeated --arg key=value options into typed arguments.

    Types come from the action's declared parameters, so "amount=50" becomes a
    number for an action that declares one and stays a string for one that does not.
    """
    arguments: dict[str, object] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--arg expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        parameter = spec_parameters.get(key)
        declared = parameter.type if parameter else "string"
        if declared == "number":
            arguments[key] = float(raw)
        elif declared == "integer":
            arguments[key] = int(raw)
        elif declared == "boolean":
            arguments[key] = raw.strip().lower() in {"1", "true", "yes"}
        else:
            arguments[key] = raw
    return arguments


def _report_refusal(refusal: ActionRefusal) -> int:
    print(f"REFUSED: {refusal.reason}", file=sys.stderr)
    print(f"  {refusal.explanation}", file=sys.stderr)
    if refusal.detail:
        print(f"  {refusal.detail}", file=sys.stderr)
    return 1


def _print_proposal(proposal: Proposal) -> None:
    print(f"proposal {proposal.id}   [{proposal.state}]")
    print(f"  action:    {proposal.action_id}")
    print(f"  proposer:  {proposal.principal_id}   purpose: {proposal.purpose}")
    print(f"  arguments: {proposal.arguments}")
    print(f"  blast:     {proposal.blast.headline()}")
    print(f"  rules:     {', '.join(proposal.matched_rules) or '(none)'}")
    if proposal.approval:
        verdict = "approved" if proposal.approval.approved else "denied"
        print(f"  {verdict} by {proposal.approval.decided_by}: {proposal.approval.note}")
    if proposal.state == "pending_approval":
        print("\n  A human must approve this before it can run:")
        print(f"    aperture actions approve {proposal.id} --as <approver-id>")


def cmd_actions(args: argparse.Namespace) -> int:
    """Propose, approve, execute, and roll back governed actions."""
    workspace = Workspace.load(args.workspace)
    gateway = workspace.gateway()
    action = args.action_command

    if action == "list":
        visible = gateway.list_actions(args.principal, args.purpose)
        if args.json:
            _print_json(visible)
            return 0
        if not visible:
            print(f"{args.principal} may take no actions under purpose '{args.purpose}'.")
            return 0
        print(f"Actions available to {args.principal} under '{args.purpose}':")
        for entry in visible:
            flags = []
            flags.append("reversible" if entry["reversible"] else "IRREVERSIBLE")
            if entry["requires_approval"]:
                flags.append("needs approval")
            print(f"  {entry['id']:<26} [{entry['effect_class']}] {', '.join(flags)}")
            print(f"  {'':<26} {entry['description']}")
        return 0

    if action == "propose":
        spec = workspace.actions.get(args.action)
        parameters = spec.parameters if spec else {}
        result = gateway.propose(
            args.principal, args.purpose, args.action, _parse_arguments(parameters, args.arg)
        )
        if isinstance(result, ActionRefusal):
            return _report_refusal(result)
        _print_proposal(result)
        return 0

    if action == "pending":
        proposals = workspace.action_store.list_proposals(state="pending_approval")
        if not proposals:
            print("No proposals are waiting for approval.")
            return 0
        print(f"{len(proposals)} proposal(s) awaiting approval:\n")
        for proposal in proposals:
            _print_proposal(proposal)
            print()
        return 0

    if action in {"approve", "deny"}:
        result = gateway.decide(
            args.proposal_id, args.approver, approved=(action == "approve"), note=args.note
        )
        if isinstance(result, ActionRefusal):
            return _report_refusal(result)
        _print_proposal(result)
        return 0

    if action == "execute":
        result = gateway.execute(args.proposal_id, args.principal)
        if isinstance(result, ActionRefusal):
            return _report_refusal(result)
        print(f"executed {result.id}")
        print(f"  action: {result.action_id}")
        print(f"  result: {result.result}")
        if result.reversible:
            print(f"  undo:   aperture actions rollback {result.id} -p <approver-id>")
        else:
            print("  undo:   not possible - this action is irreversible")
        return 0

    if action == "rollback":
        result = gateway.rollback(args.execution_id, args.principal)
        if isinstance(result, ActionRefusal):
            return _report_refusal(result)
        print(f"rolled back {result.id}")
        print(f"  result: {result.rollback_result}")
        return 0

    if action == "stuck":
        stranded = workspace.action_store.stuck_executions(args.older_than)
        if not stranded:
            print("No proposals are stranded mid-execution.")
            return 0
        print(f"{len(stranded)} proposal(s) stranded mid-execution.")
        print("These are NOT retried automatically: the action's outcome is unknown,")
        print("so retrying may double-charge and abandoning may strand an operation.\n")
        for proposal in stranded:
            print(f"  {proposal.id}  {proposal.action_id}  by {proposal.principal_id}")
            print(f"    {proposal.blast.headline()}")
        return 1

    if action == "history":
        records: list[ExecutionRecord] = workspace.action_store.list_executions()
        if not records:
            print("No actions have been executed.")
            return 0
        for record in records:
            state = "rolled back" if record.rolled_back_at else "executed"
            print(f"{record.executed_at}  {record.id}  {record.action_id:<24} "
                  f"{record.principal_id:<20} {state}")
        return 0

    raise ValueError(f"unknown action command: {action}")


def cmd_policy(args: argparse.Namespace) -> int:
    """Show or diff the effective access a policy grants."""
    import yaml

    from .policy import Policy
    from .policy_diff import compute_access, diff_access

    workspace = Workspace.load(args.workspace)
    current = compute_access(
        workspace.policy, workspace.principals, workspace.catalog, workspace.actions
    )

    if args.policy_command == "access":
        if args.json:
            _print_json(
                [
                    {
                        "principal": g.principal_id,
                        "purpose": g.purpose,
                        "kind": g.kind,
                        "target": g.target,
                    }
                    for g in sorted(current.grants, key=lambda g: g.describe())
                ]
            )
            return 0
        print(f"{len(current)} effective grant(s):")
        for grant in sorted(current.grants, key=lambda g: g.describe()):
            print(f"  {grant.describe()}")
        return 0

    # diff
    other_path = Path(args.against)
    if not other_path.is_file():
        print(f"no such policy file: {other_path}", file=sys.stderr)
        return 1
    try:
        other_policy = Policy.from_dict(yaml.safe_load(other_path.read_text(encoding="utf-8")) or {})
    except Exception as exc:  # noqa: BLE001 - report, do not traceback at an operator
        print(f"could not load {other_path}: {exc}", file=sys.stderr)
        return 1

    baseline = compute_access(
        other_policy, workspace.principals, workspace.catalog, workspace.actions
    )
    # The file passed in is the "before"; the workspace is the proposed "after".
    difference = diff_access(baseline, current)
    print(difference.render())

    if difference.widened and not args.allow_widening:
        print(
            f"\nRefusing: this change grants {len(difference.widened)} new access path(s).",
            file=sys.stderr,
        )
        print(
            "Re-run with --allow-widening if the widening is intended and reviewed.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the MCP server over stdio."""
    from .mcp_server import serve_stdio

    secret = os.environ.get(args.signing_secret_env) if args.signing_secret_env else None
    if args.signing_secret_env and not secret:
        print(
            f"identity mode 'signed' requires {args.signing_secret_env} to be set",
            file=sys.stderr,
        )
        return 1

    serve_stdio(
        workspace_root=args.workspace,
        principal_id=args.principal,
        default_purpose=args.purpose,
        allow_principal_override=args.allow_principal_override,
        signing_secret=secret,
        jwks_url=args.jwks_url,
        issuer=args.issuer,
        audience=args.audience,
    )
    return 0


def cmd_assertion(args: argparse.Namespace) -> int:
    """Mint a signed caller assertion.

    Belongs to whatever issues identity in a real deployment - a gateway or IdP
    exchange. Exposed here so the flow is testable and demonstrable end to end.
    """
    from .assertions import sign_assertion

    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"{args.secret_env} is not set", file=sys.stderr)
        return 1
    print(sign_assertion(secret, args.principal, args.purpose, ttl_seconds=args.ttl))
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="aperture",
        description="Governed context plane for AI agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_workspace(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "-w", "--workspace", type=Path, default=DEFAULT_WORKSPACE,
            help="workspace directory (default: ./workspace)",
        )

    demo = subparsers.add_parser("demo", help="create a demo workspace")
    demo.add_argument("--path", type=Path, default=DEFAULT_WORKSPACE)
    demo.set_defaults(func=cmd_demo)

    lint = subparsers.add_parser("lint", help="validate catalog, policy, and principals")
    add_workspace(lint)
    lint.set_defaults(func=cmd_lint)

    sources = subparsers.add_parser("sources", help="list sources visible to a principal")
    add_workspace(sources)
    sources.add_argument("-p", "--principal", required=True)
    sources.add_argument("--purpose", required=True)
    sources.add_argument("--json", action="store_true")
    sources.set_defaults(func=cmd_sources)

    query = subparsers.add_parser("query", help="run a governed retrieval")
    add_workspace(query)
    query.add_argument("question")
    query.add_argument("-p", "--principal", required=True)
    query.add_argument("--purpose", required=True)
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--budget", type=int, default=6000)
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=cmd_query)

    explain = subparsers.add_parser("explain", help="show the lineage entry for a trace id")
    add_workspace(explain)
    explain.add_argument("trace_id")
    explain.set_defaults(func=cmd_explain)

    lineage = subparsers.add_parser("lineage", help="inspect or verify the access log")
    add_workspace(lineage)
    lineage.add_argument(
        "action", choices=["tail", "verify", "checkpoint", "export"], default="tail", nargs="?"
    )
    lineage.add_argument("--limit", type=int, default=20)
    lineage.add_argument("--since", type=int, default=0, help="export entries after this seq")
    lineage.add_argument(
        "--secret-env", default=None, metavar="VAR",
        help="environment variable holding the checkpoint signing secret",
    )
    lineage.set_defaults(func=cmd_lineage)

    actions = subparsers.add_parser("actions", help="govern what agents may do")
    add_workspace(actions)
    action_subparsers = actions.add_subparsers(dest="action_command", required=True)
    actions.set_defaults(func=cmd_actions)

    act_list = action_subparsers.add_parser("list", help="actions a principal may take")
    act_list.add_argument("-p", "--principal", required=True)
    act_list.add_argument("--purpose", required=True)
    act_list.add_argument("--json", action="store_true")

    act_propose = action_subparsers.add_parser("propose", help="price and register an action")
    act_propose.add_argument("action", help="action id, e.g. support.refund")
    act_propose.add_argument("-p", "--principal", required=True)
    act_propose.add_argument("--purpose", required=True)
    act_propose.add_argument(
        "--arg", action="append", default=[], metavar="KEY=VALUE",
        help="action argument; repeat for each one",
    )

    action_subparsers.add_parser("pending", help="proposals awaiting human approval")

    act_approve = action_subparsers.add_parser("approve", help="approve a pending proposal")
    act_approve.add_argument("proposal_id")
    act_approve.add_argument("--as", dest="approver", required=True, help="approving identity")
    act_approve.add_argument("--note", default="")

    act_deny = action_subparsers.add_parser("deny", help="reject a pending proposal")
    act_deny.add_argument("proposal_id")
    act_deny.add_argument("--as", dest="approver", required=True)
    act_deny.add_argument("--note", default="")

    act_execute = action_subparsers.add_parser("execute", help="run a cleared proposal")
    act_execute.add_argument("proposal_id")
    act_execute.add_argument("-p", "--principal", required=True)

    act_rollback = action_subparsers.add_parser("rollback", help="undo an execution")
    act_rollback.add_argument("execution_id")
    act_rollback.add_argument("-p", "--principal", required=True)

    action_subparsers.add_parser("history", help="everything that has been executed")

    act_stuck = action_subparsers.add_parser(
        "stuck", help="proposals stranded mid-execution, for a human to resolve"
    )
    act_stuck.add_argument("--older-than", type=int, default=300, metavar="SECONDS")

    policy = subparsers.add_parser(
        "policy", help="inspect what the policy actually grants"
    )
    add_workspace(policy)
    policy_subparsers = policy.add_subparsers(dest="policy_command", required=True)
    policy.set_defaults(func=cmd_policy)

    pol_access = policy_subparsers.add_parser(
        "access", help="every principal, purpose, and thing they can reach"
    )
    pol_access.add_argument("--json", action="store_true")

    pol_diff = policy_subparsers.add_parser(
        "diff",
        help="compare effective access against another policy file; exits 1 on widening",
    )
    pol_diff.add_argument("--against", required=True, metavar="POLICY_YAML",
                          help="the previous policy to compare against")
    pol_diff.add_argument("--allow-widening", action="store_true",
                          help="permit new access paths (use when the widening is intended)")

    serve = subparsers.add_parser("serve", help="run the MCP server over stdio")
    add_workspace(serve)
    serve.add_argument(
        "-p", "--principal", required=True,
        help="identity this server instance acts as",
    )
    serve.add_argument(
        "--purpose", default=None,
        help="default purpose when a tool call omits one",
    )
    serve.add_argument(
        "--allow-principal-override", action="store_true",
        help="permit tool calls to specify a principal (local development only)",
    )
    serve.add_argument(
        "--signing-secret-env", default=None, metavar="VAR",
        help=(
            "environment variable holding the assertion signing secret; when set, "
            "every tool call must carry a signed caller assertion"
        ),
    )
    serve.add_argument(
        "--jwks-url", default=None,
        help="verify RS256 tokens from an identity provider's JWKS endpoint",
    )
    serve.add_argument("--issuer", default=None, help="expected token issuer")
    serve.add_argument("--audience", default=None, help="expected token audience")
    serve.set_defaults(func=cmd_serve)

    assertion = subparsers.add_parser(
        "assertion", help="mint a signed caller assertion (issuer side)"
    )
    assertion.add_argument("-p", "--principal", required=True)
    assertion.add_argument("--purpose", required=True)
    assertion.add_argument("--ttl", type=int, default=120, help="seconds until expiry")
    assertion.add_argument("--secret-env", default="APERTURE_SIGNING_SECRET", metavar="VAR")
    assertion.set_defaults(func=cmd_assertion)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WorkspaceError as exc:
        print(f"workspace error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
