"""Effective-access diffing.

The question a reviewer needs answered is not "which lines changed" but "who can
now reach something they could not reach before". These tests pin the difference
between those two questions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aperture.policy import Policy
from aperture.policy_diff import compute_access, diff_access
from aperture.workspace import Workspace


def access_for(workspace: Workspace, policy: Policy):
    return compute_access(policy, workspace.principals, workspace.catalog, workspace.actions)


def edited(workspace: Workspace, replace: tuple[str, str]) -> Policy:
    """Load the workspace policy with one textual substitution applied."""
    raw = (workspace.root / "policy.yaml").read_text(encoding="utf-8")
    old, new = replace
    assert old in raw, "fixture text not found; the demo policy changed"
    return Policy.from_dict(yaml.safe_load(raw.replace(old, new)))


# --------------------------------------------------------------------------- #
# the matrix
# --------------------------------------------------------------------------- #


def test_matrix_covers_sources_and_actions(workspace: Workspace) -> None:
    matrix = access_for(workspace, workspace.policy)
    kinds = {grant.kind for grant in matrix.grants}
    assert kinds == {"source", "action"}
    assert len(matrix) > 20


def test_matrix_matches_the_runtime_decision(workspace: Workspace) -> None:
    """Enumeration goes through the same evaluator the runtime uses.

    A separate model of what the rules mean would eventually disagree with the
    enforcement path, and the disagreement would be invisible until it mattered.
    """
    from aperture.plane import ContextPlane

    plane = ContextPlane(workspace)
    matrix = access_for(workspace, workspace.policy)

    for principal_id in workspace.principals.ids():
        for purpose in workspace.policy.purposes:
            from_runtime = {s["id"] for s in plane.list_sources(principal_id, purpose)}
            from_matrix = {
                grant.target
                for grant in matrix.grants
                if grant.principal_id == principal_id
                and grant.purpose == purpose
                and grant.kind == "source"
            }
            assert from_runtime == from_matrix, (principal_id, purpose)


# --------------------------------------------------------------------------- #
# diffing
# --------------------------------------------------------------------------- #


def test_identical_policies_produce_no_diff(workspace: Workspace) -> None:
    matrix = access_for(workspace, workspace.policy)
    difference = diff_access(matrix, matrix)
    assert difference.changed is False
    assert "No change" in difference.render()


def test_a_widening_is_detected(workspace: Workspace) -> None:
    before = access_for(workspace, workspace.policy)
    after = access_for(
        workspace,
        edited(
            workspace,
            (
                "      groups: [hr]\n      purposes: [hr_support]\n      sources: [people_db]",
                "      groups: [hr, support]\n      purposes: [hr_support, customer_support]\n"
                "      sources: [people_db]",
            ),
        ),
    )
    difference = diff_access(before, after)
    assert difference.widened
    assert not difference.narrowed
    targets = {grant.target for grant in difference.widened}
    assert targets == {"people_db"}
    assert "WIDENED" in difference.render()


def test_a_narrowing_is_detected_and_not_treated_as_safe_silence(workspace: Workspace) -> None:
    before = access_for(workspace, workspace.policy)
    after = access_for(
        workspace, edited(workspace, ("      groups: [hr]\n      purposes: [hr_support]", "      groups: [nobody]\n      purposes: [hr_support]"))
    )
    difference = diff_access(before, after)
    assert difference.narrowed
    assert not difference.widened
    assert "NARROWED" in difference.render()


def test_widening_and_narrowing_are_never_netted_off(workspace: Workspace) -> None:
    """A change that removes one grant and adds another is not 'no change'."""
    before = access_for(workspace, workspace.policy)
    after = access_for(
        workspace,
        edited(
            workspace,
            (
                "      groups: [support]\n      purposes: [customer_support]\n"
                "      actions: [support.close_ticket]",
                "      groups: [engineering]\n      purposes: [customer_support]\n"
                "      actions: [support.close_ticket]",
            ),
        ),
    )
    difference = diff_access(before, after)
    assert difference.widened, "engineering gained the action"
    assert difference.narrowed, "support lost it"
    assert difference.changed


def test_a_rule_refactor_that_grants_nothing_new_shows_no_change(workspace: Workspace) -> None:
    """The tool is indifferent to how policy is written, interested only in effect.

    Renaming a rule and reordering its match keys changes the file substantially
    and the permissions not at all.
    """
    raw = (workspace.root / "policy.yaml").read_text(encoding="utf-8")
    refactored = raw.replace("id: hr-reads-handbook", "id: hr_handbook_read_access")
    after = Policy.from_dict(yaml.safe_load(refactored))

    difference = diff_access(
        access_for(workspace, workspace.policy), access_for(workspace, after)
    )
    assert difference.changed is False


# --------------------------------------------------------------------------- #
# the CLI gate
# --------------------------------------------------------------------------- #


def run_cli(argv: list[str]) -> int:
    from aperture.cli import main

    return main(argv)


def test_diff_command_exits_zero_when_nothing_changed(
    workspace_root: Path, tmp_path: Path, capsys
) -> None:
    baseline = tmp_path / "before.yaml"
    baseline.write_text((workspace_root / "policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    code = run_cli(["policy", "-w", str(workspace_root), "diff", "--against", str(baseline)])
    assert code == 0
    assert "No change" in capsys.readouterr().out


def test_diff_command_fails_the_build_on_widening(
    workspace_root: Path, tmp_path: Path, capsys
) -> None:
    """This is the point of the command: CI refuses a policy change that widens access."""
    baseline = tmp_path / "before.yaml"
    baseline.write_text((workspace_root / "policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    policy_path = workspace_root / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "      groups: [hr]\n      purposes: [hr_support]\n      sources: [people_db]",
            "      groups: [hr, support]\n      purposes: [hr_support, customer_support]\n"
            "      sources: [people_db]",
        ),
        encoding="utf-8",
    )

    code = run_cli(["policy", "-w", str(workspace_root), "diff", "--against", str(baseline)])
    captured = capsys.readouterr()
    assert code == 1
    assert "WIDENED" in captured.out
    assert "Refusing" in captured.err


def test_widening_can_be_accepted_deliberately(
    workspace_root: Path, tmp_path: Path
) -> None:
    baseline = tmp_path / "before.yaml"
    baseline.write_text((workspace_root / "policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    policy_path = workspace_root / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "      groups: [hr]\n      purposes: [hr_support]\n      sources: [people_db]",
            "      groups: [hr, support]\n      purposes: [hr_support, customer_support]\n"
            "      sources: [people_db]",
        ),
        encoding="utf-8",
    )
    code = run_cli(
        ["policy", "-w", str(workspace_root), "diff", "--against", str(baseline),
         "--allow-widening"]
    )
    assert code == 0


def test_missing_baseline_is_reported(workspace_root: Path, capsys) -> None:
    code = run_cli(
        ["policy", "-w", str(workspace_root), "diff", "--against", "does-not-exist.yaml"]
    )
    assert code == 1
    assert "no such policy file" in capsys.readouterr().err


def test_access_command_lists_every_grant(workspace_root: Path, capsys) -> None:
    assert run_cli(["policy", "-w", str(workspace_root), "access"]) == 0
    out = capsys.readouterr().out
    assert "effective grant(s)" in out
    assert "-> source" in out and "-> action" in out
