"""Command-line control plane.

Covers the operator loop: build a workspace, lint the policy, ask a question as a
given identity, explain what happened, and verify the lineage chain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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

    if args.action == "verify":
        ok, problems = log.verify()
        entries = len(list(log.read_all()))
        if ok:
            print(f"Lineage chain intact across {entries} entries.")
            return 0
        print(f"LINEAGE CHAIN BROKEN ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    for entry in log.tail(args.limit):
        withheld = ", ".join(f"{w['count']}x{w['reason']}" for w in entry.get("withheld", []))
        print(
            f"{entry['ts']}  {entry['trace_id']}  {entry['principal_id']:<18} "
            f"{entry['purpose']:<18} returned={len(entry.get('returned', []))}"
            f"{('  withheld=' + withheld) if withheld else ''}"
        )
        print(f"    q: {entry['question']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the MCP server over stdio."""
    from .mcp_server import serve_stdio

    serve_stdio(
        workspace_root=args.workspace,
        principal_id=args.principal,
        default_purpose=args.purpose,
        allow_principal_override=args.allow_principal_override,
    )
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
    lineage.add_argument("action", choices=["tail", "verify"], default="tail", nargs="?")
    lineage.add_argument("--limit", type=int, default=20)
    lineage.set_defaults(func=cmd_lineage)

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
    serve.set_defaults(func=cmd_serve)

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
