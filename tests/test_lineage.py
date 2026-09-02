"""Lineage chain integrity."""

from __future__ import annotations

import json
from pathlib import Path

from aperture.lineage import GENESIS_HASH, LineageLog
from aperture.plane import ContextPlane
from aperture.types import SearchRequest


def test_first_entry_chains_to_genesis(tmp_path: Path) -> None:
    log = LineageLog(tmp_path / "access.jsonl")
    entry = log.append({"trace_id": "t1"})
    assert entry["prev_hash"] == GENESIS_HASH
    assert entry["seq"] == 1


def test_entries_chain_in_sequence(tmp_path: Path) -> None:
    log = LineageLog(tmp_path / "access.jsonl")
    first = log.append({"trace_id": "t1"})
    second = log.append({"trace_id": "t2"})
    assert second["prev_hash"] == first["hash"]
    assert second["seq"] == 2
    ok, problems = log.verify()
    assert ok, problems


def test_verify_detects_edited_content(tmp_path: Path) -> None:
    """Editing a historical entry must be detectable."""
    path = tmp_path / "access.jsonl"
    log = LineageLog(path)
    log.append({"trace_id": "t1", "principal_id": "u_kim"})
    log.append({"trace_id": "t2", "principal_id": "u_kim"})

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["principal_id"] = "u_dana"
    lines[0] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problems = log.verify()
    assert not ok
    assert any("altered" in problem for problem in problems)


def test_verify_detects_a_deleted_entry(tmp_path: Path) -> None:
    """Removing a line breaks both the sequence and the hash chain."""
    path = tmp_path / "access.jsonl"
    log = LineageLog(path)
    for index in range(3):
        log.append({"trace_id": f"t{index}"})

    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problems = log.verify()
    assert not ok
    assert problems


def test_empty_log_verifies(tmp_path: Path) -> None:
    ok, problems = LineageLog(tmp_path / "access.jsonl").verify()
    assert ok and problems == []


def test_every_query_is_logged(plane: ContextPlane) -> None:
    response = plane.search("u_kim", SearchRequest(question="refund window", purpose="customer_support"))
    entry = plane.explain(response.trace_id)
    assert entry is not None
    assert entry["principal_id"] == "u_kim"
    assert entry["purpose"] == "customer_support"
    assert entry["question"] == "refund window"
    ok, problems = plane.workspace.lineage.verify()
    assert ok, problems


def test_denied_queries_are_logged_too(plane: ContextPlane) -> None:
    """An access attempt that returned nothing is exactly what auditors look for."""
    response = plane.search(
        "u_raj", SearchRequest(question="termination severance schedule", purpose="hr_support")
    )
    entry = plane.explain(response.trace_id)
    assert entry is not None
    assert entry["returned"] == []
    assert entry["withheld"]


def test_unknown_principal_attempt_is_logged(plane: ContextPlane) -> None:
    response = plane.search("mallory", SearchRequest(question="salaries", purpose="hr_support"))
    entry = plane.explain(response.trace_id)
    assert entry is not None
    assert entry["principal_id"] == "mallory"
    assert entry["tenant"] is None
