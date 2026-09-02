"""Durable state for proposals and executions.

Proposals outlive a single request - that is the whole point of a human approval
step - so they need somewhere to live. This is a small JSON-file store with atomic
writes, which is enough for a single-node deployment and honest about being so.

Writes are atomic (temp file plus replace) because a half-written proposals file
would leave a pending approval in an unreadable state, and the gateway's failure
mode has to be "cannot act", never "acts on garbage".
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .types import ExecutionRecord, Proposal


def new_proposal_id() -> str:
    """Generate a proposal id."""
    return f"prp_{uuid.uuid4().hex[:16]}"


def new_execution_id() -> str:
    """Generate an execution id."""
    return f"exe_{uuid.uuid4().hex[:16]}"


class ActionStore:
    """Reads and writes proposals and execution records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.proposals_path = self.root / "proposals.json"
        self.executions_path = self.root / "executions.json"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- low level -------------------------------------------------------- #

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        return json.loads(text)

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        """Write atomically so a crash cannot leave a partial file behind."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        )
        try:
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)

    # -- proposals -------------------------------------------------------- #

    def save_proposal(self, proposal: Proposal) -> Proposal:
        """Insert or update a proposal."""
        data = self._read(self.proposals_path)
        data[proposal.id] = json.loads(proposal.model_dump_json())
        self._write(self.proposals_path, data)
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Return a proposal by id, or None."""
        raw = self._read(self.proposals_path).get(proposal_id)
        return Proposal.model_validate(raw) if raw else None

    def list_proposals(self, state: str | None = None) -> list[Proposal]:
        """All proposals, newest first, optionally filtered by state."""
        proposals = [
            Proposal.model_validate(raw) for raw in self._read(self.proposals_path).values()
        ]
        if state:
            proposals = [p for p in proposals if str(p.state) == state]
        return sorted(proposals, key=lambda p: p.created_at, reverse=True)

    # -- executions ------------------------------------------------------- #

    def save_execution(self, record: ExecutionRecord) -> ExecutionRecord:
        """Insert or update an execution record."""
        data = self._read(self.executions_path)
        data[record.id] = json.loads(record.model_dump_json())
        self._write(self.executions_path, data)
        return record

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Return an execution record by id, or None."""
        raw = self._read(self.executions_path).get(execution_id)
        return ExecutionRecord.model_validate(raw) if raw else None

    def list_executions(self) -> list[ExecutionRecord]:
        """All execution records, newest first."""
        records = [
            ExecutionRecord.model_validate(raw)
            for raw in self._read(self.executions_path).values()
        ]
        return sorted(records, key=lambda r: r.executed_at, reverse=True)
