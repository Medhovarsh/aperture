"""Tamper-evident access lineage.

Every query the plane serves appends one line to an append-only JSONL log. Each
entry stores the hash of the entry before it, so the file is a hash chain: editing
or deleting any historical line invalidates every hash after it, and
:meth:`LineageLog.verify` will say exactly where the break is.

This is the artifact an auditor asks for. It answers, for any answer an agent gave,
which identity asked, under what declared purpose, which sources were consulted,
which records were returned, and what was withheld and why.

The chain is tamper-*evident*, not tamper-*proof*: an attacker with write access can
rewrite the whole file consistently. Detecting that requires anchoring the head hash
somewhere the attacker does not control, which is a v2 concern and is not claimed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON encoding used as hash input."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(payload: dict[str, Any], prev_hash: str) -> str:
    """Hash an entry body together with the previous entry's hash."""
    body = _canonical(payload)
    return hashlib.sha256(f"{prev_hash}\n{body}".encode("utf-8")).hexdigest()


def new_trace_id() -> str:
    """Generate a trace id for one query."""
    return f"trc_{uuid.uuid4().hex[:16]}"


class LineageLog:
    """Append-only, hash-chained query log backed by a JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writing ---------------------------------------------------------- #

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an entry, chaining it to the current head.

        The write is flushed and fsynced before returning, so a crash cannot lose an
        access record that a caller was already told about.
        """
        prev_hash, seq = self._head()
        entry = {
            "seq": seq + 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            **payload,
            "prev_hash": prev_hash,
        }
        entry["hash"] = compute_hash(entry, prev_hash)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def _head(self) -> tuple[str, int]:
        """Return the hash and sequence number of the last entry."""
        last: dict[str, Any] | None = None
        for last in self.read_all():
            pass
        if last is None:
            return GENESIS_HASH, 0
        return str(last.get("hash", GENESIS_HASH)), int(last.get("seq", 0))

    # -- reading ---------------------------------------------------------- #

    def read_all(self) -> Iterator[dict[str, Any]]:
        """Yield every entry in order. Missing file yields nothing."""
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def find(self, trace_id: str) -> dict[str, Any] | None:
        """Return the entry for a trace id, or None."""
        for entry in self.read_all():
            if entry.get("trace_id") == trace_id:
                return entry
        return None

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent entries, oldest first."""
        entries = list(self.read_all())
        return entries[-limit:]

    # -- integrity -------------------------------------------------------- #

    def verify(self) -> tuple[bool, list[str]]:
        """Recompute the chain and report every inconsistency found.

        Returns:
            A tuple of (ok, problems). ``ok`` is True only when the chain is intact
            from genesis to head.
        """
        problems: list[str] = []
        prev_hash = GENESIS_HASH
        expected_seq = 1

        for line_number, entry in enumerate(self.read_all(), 1):
            stored_hash = entry.get("hash")
            body = {k: v for k, v in entry.items() if k != "hash"}

            if entry.get("prev_hash") != prev_hash:
                problems.append(
                    f"line {line_number}: prev_hash does not match the preceding entry"
                )
            if entry.get("seq") != expected_seq:
                problems.append(
                    f"line {line_number}: expected seq {expected_seq}, found {entry.get('seq')}"
                )
            recomputed = compute_hash(body, str(entry.get("prev_hash", GENESIS_HASH)))
            if recomputed != stored_hash:
                problems.append(f"line {line_number}: entry content has been altered")

            prev_hash = str(stored_hash)
            expected_seq = int(entry.get("seq", expected_seq)) + 1

        return (not problems), problems
