"""Durable state for proposals and executions.

Backed by SQLite in WAL mode. The store's job is not merely to remember proposals -
it is to make executing one an *atomic claim*, so that concurrent callers cannot
both act on the same approval.

That guarantee is the whole reason this is a database rather than a JSON file. An
earlier file-backed version did read-modify-write, which let eight concurrent
``execute`` calls on one approved 50 USD refund write eight refund rows. The state
check passed in all eight threads before any of them wrote back.

The fix is :meth:`ActionStore.claim_for_execution`: a single conditional UPDATE that
moves a proposal from ``ready`` to ``executing`` only if it is still ``ready``.
SQLite serializes it, so exactly one caller can win. Losers are told the proposal is
already in flight rather than being allowed to act.

A proposal left in ``executing`` after a crash is deliberately *not* auto-recovered.
An action whose outcome is unknown must be looked at by a human, because retrying it
may double-charge and abandoning it may strand a half-finished operation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .types import ExecutionRecord, Proposal, ProposalState

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS proposals (
    id                TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    principal_id      TEXT NOT NULL,
    purpose           TEXT NOT NULL,
    action_id         TEXT NOT NULL,
    arguments         TEXT NOT NULL,
    arguments_hash    TEXT NOT NULL,
    blast             TEXT NOT NULL,
    state             TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    matched_rules     TEXT NOT NULL,
    approval          TEXT,
    execution_id      TEXT
);
CREATE INDEX IF NOT EXISTS proposals_state ON proposals(state);
CREATE INDEX IF NOT EXISTS proposals_principal ON proposals(principal_id, created_at);

CREATE TABLE IF NOT EXISTS executions (
    id              TEXT PRIMARY KEY,
    proposal_id     TEXT NOT NULL,
    action_id       TEXT NOT NULL,
    principal_id    TEXT NOT NULL,
    executed_at     TEXT NOT NULL,
    amount          REAL NOT NULL DEFAULT 0,
    affected        INTEGER NOT NULL DEFAULT 0,
    result          TEXT NOT NULL,
    compensation    TEXT,
    rolled_back_at  TEXT,
    rollback_result TEXT
);
CREATE INDEX IF NOT EXISTS executions_spend
    ON executions(principal_id, action_id, executed_at);

CREATE TABLE IF NOT EXISTS seen_nonces (
    nonce      TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
"""


def new_proposal_id() -> str:
    """Generate a proposal id."""
    return f"prp_{uuid.uuid4().hex[:16]}"


def new_execution_id() -> str:
    """Generate an execution id."""
    return f"exe_{uuid.uuid4().hex[:16]}"


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


class ActionStore:
    """Transactional storage for proposals, executions, and replay nonces."""

    def __init__(self, root: Path, timeout: float = 10.0) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "actions.db"
        self.timeout = timeout
        self._local = threading.local()
        self._initialize()

    # -- connections ------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        """One connection per thread, in WAL mode with a busy timeout.

        WAL lets readers run while a writer holds the lock, and the busy timeout
        makes concurrent writers queue instead of failing immediately.
        """
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            self._local.connection = connection
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        connection.executescript(_SCHEMA)
        row = connection.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def close(self) -> None:
        """Close this thread's connection, if it has one."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    # -- serialization ---------------------------------------------------- #

    @staticmethod
    def _to_row(proposal: Proposal) -> tuple:
        return (
            proposal.id,
            proposal.created_at.isoformat(),
            proposal.principal_id,
            proposal.purpose,
            proposal.action_id,
            _dumps(proposal.arguments),
            proposal.arguments_hash,
            proposal.blast.model_dump_json(),
            str(proposal.state),
            int(proposal.requires_approval),
            _dumps(list(proposal.matched_rules)),
            proposal.approval.model_dump_json() if proposal.approval else None,
            proposal.execution_id,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Proposal:
        return Proposal.model_validate(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "principal_id": row["principal_id"],
                "purpose": row["purpose"],
                "action_id": row["action_id"],
                "arguments": json.loads(row["arguments"]),
                "arguments_hash": row["arguments_hash"],
                "blast": json.loads(row["blast"]),
                "state": row["state"],
                "requires_approval": bool(row["requires_approval"]),
                "matched_rules": json.loads(row["matched_rules"]),
                "approval": json.loads(row["approval"]) if row["approval"] else None,
                "execution_id": row["execution_id"],
            }
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord.model_validate(
            {
                "id": row["id"],
                "proposal_id": row["proposal_id"],
                "action_id": row["action_id"],
                "principal_id": row["principal_id"],
                "executed_at": row["executed_at"],
                "result": json.loads(row["result"]),
                "compensation": json.loads(row["compensation"]) if row["compensation"] else None,
                "rolled_back_at": row["rolled_back_at"],
                "rollback_result": (
                    json.loads(row["rollback_result"]) if row["rollback_result"] else None
                ),
            }
        )

    # -- proposals -------------------------------------------------------- #

    def save_proposal(self, proposal: Proposal) -> Proposal:
        """Insert or replace a proposal."""
        self._connect().execute(
            "INSERT OR REPLACE INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self._to_row(proposal),
        )
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Return a proposal by id, or None."""
        row = self._connect().execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def list_proposals(self, state: str | None = None) -> list[Proposal]:
        """All proposals, newest first, optionally filtered by state."""
        connection = self._connect()
        if state:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE state = ? ORDER BY created_at DESC", (state,)
            )
        else:
            rows = connection.execute("SELECT * FROM proposals ORDER BY created_at DESC")
        return [self._from_row(row) for row in rows]

    def transition(
        self, proposal_id: str, expected: Iterable[ProposalState], new_state: ProposalState
    ) -> bool:
        """Atomically move a proposal between states.

        Returns True only for the caller that actually performed the transition, so
        callers can use it as a mutual-exclusion primitive.
        """
        allowed = [str(state) for state in expected]
        placeholders = ",".join("?" * len(allowed))
        cursor = self._connect().execute(
            f"UPDATE proposals SET state = ? WHERE id = ? AND state IN ({placeholders})",
            (str(new_state), proposal_id, *allowed),
        )
        return cursor.rowcount == 1

    def claim_for_execution(self, proposal_id: str) -> bool:
        """Claim a ready proposal for execution. Exactly one caller can succeed.

        This is the double-spend guard. Everything about executing an action hangs
        off winning this single conditional UPDATE.
        """
        return self.transition(proposal_id, [ProposalState.READY], ProposalState.EXECUTING)

    def release_claim(self, proposal_id: str) -> bool:
        """Return a claimed proposal to ready after a pre-execution failure.

        Only used when the action provably did not run - a missing executor, a
        policy re-check that denied. Never after an executor has been entered.
        """
        return self.transition(proposal_id, [ProposalState.EXECUTING], ProposalState.READY)

    # -- executions ------------------------------------------------------- #

    def save_execution(self, record: ExecutionRecord, amount: float = 0.0, affected: int = 0) -> ExecutionRecord:
        """Insert or replace an execution record.

        ``amount`` and ``affected`` are denormalized onto the row so spend windows
        can be aggregated in SQL rather than by loading every record.
        """
        self._connect().execute(
            "INSERT OR REPLACE INTO executions "
            "(id, proposal_id, action_id, principal_id, executed_at, amount, affected, "
            " result, compensation, rolled_back_at, rollback_result) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.id,
                record.proposal_id,
                record.action_id,
                record.principal_id,
                record.executed_at.isoformat(),
                amount,
                affected,
                _dumps(record.result),
                _dumps(record.compensation) if record.compensation is not None else None,
                record.rolled_back_at.isoformat() if record.rolled_back_at else None,
                _dumps(record.rollback_result) if record.rollback_result is not None else None,
            ),
        )
        return record

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Return an execution record by id, or None."""
        row = self._connect().execute(
            "SELECT * FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        return self._execution_from_row(row) if row else None

    def list_executions(self) -> list[ExecutionRecord]:
        """All execution records, newest first."""
        rows = self._connect().execute("SELECT * FROM executions ORDER BY executed_at DESC")
        return [self._execution_from_row(row) for row in rows]

    def claim_rollback(self, execution_id: str) -> bool:
        """Claim an execution for rollback. Exactly one caller can succeed.

        Guards against two concurrent rollbacks both reversing the same refund.
        """
        cursor = self._connect().execute(
            "UPDATE executions SET rolled_back_at = ? "
            "WHERE id = ? AND rolled_back_at IS NULL AND compensation IS NOT NULL",
            (datetime.now(timezone.utc).isoformat(), execution_id),
        )
        return cursor.rowcount == 1

    def record_rollback_result(self, execution_id: str, result: dict[str, Any]) -> None:
        """Attach the compensating operation's result to a claimed rollback."""
        self._connect().execute(
            "UPDATE executions SET rollback_result = ? WHERE id = ?",
            (_dumps(result), execution_id),
        )

    def undo_rollback_claim(self, execution_id: str) -> None:
        """Release a rollback claim whose compensating call failed."""
        self._connect().execute(
            "UPDATE executions SET rolled_back_at = NULL WHERE id = ?", (execution_id,)
        )

    # -- spend windows ---------------------------------------------------- #

    def spend_since(
        self, principal_id: str, action_id: str, since: datetime
    ) -> tuple[float, int]:
        """Total amount and action count for one principal since a point in time.

        Rolled-back executions still count. An action that was performed and then
        reversed consumed real capacity and, for a refund, really moved money twice.
        """
        row = self._connect().execute(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count "
            "FROM executions WHERE principal_id = ? AND action_id = ? AND executed_at >= ?",
            (principal_id, action_id, since.isoformat()),
        ).fetchone()
        return float(row["total"]), int(row["count"])

    # -- replay protection ------------------------------------------------ #

    def remember_nonce(self, nonce: str, expires_at: datetime) -> bool:
        """Record a nonce, returning False if it has been seen before.

        Used by signed caller assertions so a captured token cannot be replayed.
        """
        self._connect().execute(
            "DELETE FROM seen_nonces WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        try:
            self._connect().execute(
                "INSERT INTO seen_nonces (nonce, expires_at) VALUES (?,?)",
                (nonce, expires_at.isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    # -- maintenance ------------------------------------------------------ #

    def stuck_executions(self, older_than_seconds: int = 300) -> list[Proposal]:
        """Proposals stranded mid-execution, for an operator to inspect.

        These are never retried automatically: the outcome of the underlying action
        is unknown, so a machine cannot safely decide between retrying and giving up.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        rows = self._connect().execute(
            "SELECT * FROM proposals WHERE state = ? AND created_at < ?",
            (str(ProposalState.EXECUTING), cutoff),
        )
        return [self._from_row(row) for row in rows]
