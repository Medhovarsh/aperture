"""Postgres store, for deployments that span machines.

SQLite makes execution atomic across processes on one host. That is enough until
the day there are two hosts, and on that day the execution claim silently stops
being a claim: two workers with two SQLite files both believe they won.

This backend moves the consistency domain into a database every worker shares, so
the same guarantee holds across the fleet. Nothing above the store changes,
because the contract is unchanged: :meth:`claim_for_execution` succeeds for
exactly one caller.

Behind the optional ``postgres`` extra. A single-host deployment should not have
to install a database driver to run a governance layer.

Implementation notes worth defending:

* **The claim is a plain conditional UPDATE**, not ``SELECT ... FOR UPDATE``
  followed by a write. One statement means one round trip and no window in which
  a lock is held across application code.
* **Autocommit, with each statement its own transaction.** Every operation here is
  a single statement, so a longer transaction would only widen the time a row is
  locked without making anything more consistent.
* **Timestamps are stored as ``timestamptz``.** Storing local time in a system
  that spans regions is how spend windows quietly become wrong for half the fleet.
* **Nonce insertion relies on the primary key**, so replay detection is the
  database's uniqueness guarantee rather than a read-then-write in Python.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .store import ActionStore, _dumps
from .types import ExecutionRecord, Proposal, ProposalState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id                TEXT PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL,
    principal_id      TEXT NOT NULL,
    purpose           TEXT NOT NULL,
    action_id         TEXT NOT NULL,
    arguments         JSONB NOT NULL,
    arguments_hash    TEXT NOT NULL,
    blast             JSONB NOT NULL,
    state             TEXT NOT NULL,
    requires_approval BOOLEAN NOT NULL,
    matched_rules     JSONB NOT NULL,
    approval          JSONB,
    execution_id      TEXT
);
CREATE INDEX IF NOT EXISTS proposals_state ON proposals(state);
CREATE INDEX IF NOT EXISTS proposals_principal ON proposals(principal_id, created_at);

CREATE TABLE IF NOT EXISTS executions (
    id              TEXT PRIMARY KEY,
    proposal_id     TEXT NOT NULL,
    action_id       TEXT NOT NULL,
    principal_id    TEXT NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL,
    amount          DOUBLE PRECISION NOT NULL DEFAULT 0,
    affected        INTEGER NOT NULL DEFAULT 0,
    result          JSONB NOT NULL,
    compensation    JSONB,
    rolled_back_at  TIMESTAMPTZ,
    rollback_result JSONB
);
CREATE INDEX IF NOT EXISTS executions_spend
    ON executions(principal_id, action_id, executed_at);

CREATE TABLE IF NOT EXISTS seen_nonces (
    nonce      TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL
);
"""


def _require_driver():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised only when missing
        raise RuntimeError(
            "the Postgres store needs the 'postgres' extra: "
            "pip install 'aperture-plane[postgres]'"
        ) from exc
    return psycopg, dict_row


class PostgresActionStore(ActionStore):
    """Multi-host store backed by Postgres."""

    def __init__(self, dsn: str, connect_timeout: int = 10) -> None:
        psycopg, dict_row = _require_driver()
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        self._connection = psycopg.connect(
            dsn, autocommit=True, row_factory=dict_row, connect_timeout=connect_timeout
        )
        with self._connection.cursor() as cursor:
            cursor.execute(_SCHEMA)

    def close(self) -> None:
        """Close the connection."""
        if getattr(self, "_connection", None) is not None:
            self._connection.close()

    # -- serialization ---------------------------------------------------- #

    @staticmethod
    def _proposal_from_row(row: dict[str, Any]) -> Proposal:
        return Proposal.model_validate(
            {
                **row,
                "arguments": row["arguments"],
                "blast": row["blast"],
                "matched_rules": row["matched_rules"],
                "approval": row["approval"],
            }
        )

    @staticmethod
    def _execution_from_row(row: dict[str, Any]) -> ExecutionRecord:
        return ExecutionRecord.model_validate(dict(row))

    # -- proposals -------------------------------------------------------- #

    def save_proposal(self, proposal: Proposal) -> Proposal:
        """Insert or replace a proposal."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO proposals (id, created_at, principal_id, purpose, action_id,
                    arguments, arguments_hash, blast, state, requires_approval,
                    matched_rules, approval, execution_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    state = EXCLUDED.state,
                    approval = EXCLUDED.approval,
                    execution_id = EXCLUDED.execution_id
                """,
                (
                    proposal.id,
                    proposal.created_at,
                    proposal.principal_id,
                    proposal.purpose,
                    proposal.action_id,
                    _dumps(proposal.arguments),
                    proposal.arguments_hash,
                    proposal.blast.model_dump_json(),
                    str(proposal.state),
                    proposal.requires_approval,
                    _dumps(list(proposal.matched_rules)),
                    proposal.approval.model_dump_json() if proposal.approval else None,
                    proposal.execution_id,
                ),
            )
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Return a proposal by id, or None."""
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM proposals WHERE id = %s", (proposal_id,))
            row = cursor.fetchone()
        return self._proposal_from_row(row) if row else None

    def list_proposals(self, state: str | None = None) -> list[Proposal]:
        """All proposals, newest first, optionally filtered by state."""
        with self._connection.cursor() as cursor:
            if state:
                cursor.execute(
                    "SELECT * FROM proposals WHERE state = %s ORDER BY created_at DESC",
                    (state,),
                )
            else:
                cursor.execute("SELECT * FROM proposals ORDER BY created_at DESC")
            return [self._proposal_from_row(row) for row in cursor.fetchall()]

    def transition(
        self, proposal_id: str, expected: Iterable[ProposalState], new_state: ProposalState
    ) -> bool:
        """Atomically move a proposal between states.

        One statement. Postgres decides the winner, so this holds across every
        worker sharing the database rather than only across one host's processes.
        """
        allowed = [str(state) for state in expected]
        with self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE proposals SET state = %s WHERE id = %s AND state = ANY(%s)",
                (str(new_state), proposal_id, allowed),
            )
            return cursor.rowcount == 1

    # -- executions ------------------------------------------------------- #

    def save_execution(
        self, record: ExecutionRecord, amount: float = 0.0, affected: int = 0
    ) -> ExecutionRecord:
        """Insert or replace an execution record."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO executions (id, proposal_id, action_id, principal_id,
                    executed_at, amount, affected, result, compensation,
                    rolled_back_at, rollback_result)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    rolled_back_at = EXCLUDED.rolled_back_at,
                    rollback_result = EXCLUDED.rollback_result
                """,
                (
                    record.id,
                    record.proposal_id,
                    record.action_id,
                    record.principal_id,
                    record.executed_at,
                    amount,
                    affected,
                    _dumps(record.result),
                    _dumps(record.compensation) if record.compensation is not None else None,
                    record.rolled_back_at,
                    _dumps(record.rollback_result) if record.rollback_result is not None else None,
                ),
            )
        return record

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Return an execution record by id, or None."""
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM executions WHERE id = %s", (execution_id,))
            row = cursor.fetchone()
        return self._execution_from_row(row) if row else None

    def list_executions(self) -> list[ExecutionRecord]:
        """All execution records, newest first."""
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM executions ORDER BY executed_at DESC")
            return [self._execution_from_row(row) for row in cursor.fetchall()]

    def claim_rollback(self, execution_id: str) -> bool:
        """Claim an execution for rollback. Exactly one caller can succeed."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE executions SET rolled_back_at = %s "
                "WHERE id = %s AND rolled_back_at IS NULL AND compensation IS NOT NULL",
                (datetime.now(timezone.utc), execution_id),
            )
            return cursor.rowcount == 1

    def record_rollback_result(self, execution_id: str, result: dict[str, Any]) -> None:
        """Attach the compensating operation's result to a claimed rollback."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE executions SET rollback_result = %s WHERE id = %s",
                (_dumps(result), execution_id),
            )

    def undo_rollback_claim(self, execution_id: str) -> None:
        """Release a rollback claim whose compensating call failed."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE executions SET rolled_back_at = NULL WHERE id = %s", (execution_id,)
            )

    # -- spend windows ---------------------------------------------------- #

    def spend_since(
        self, principal_id: str, action_id: str, since: datetime
    ) -> tuple[float, int]:
        """Total amount and action count for one principal since a point in time."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count "
                "FROM executions "
                "WHERE principal_id = %s AND action_id = %s AND executed_at >= %s",
                (principal_id, action_id, since),
            )
            row = cursor.fetchone()
        return float(row["total"]), int(row["count"])

    # -- replay protection ------------------------------------------------ #

    def remember_nonce(self, nonce: str, expires_at: datetime) -> bool:
        """Record a nonce, returning False if it has been seen before.

        Uniqueness is the database's job. ``ON CONFLICT DO NOTHING`` plus a rowcount
        check means two workers presenting the same token at the same instant cannot
        both be told it is fresh.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM seen_nonces WHERE expires_at < %s", (datetime.now(timezone.utc),)
            )
            cursor.execute(
                "INSERT INTO seen_nonces (nonce, expires_at) VALUES (%s, %s) "
                "ON CONFLICT (nonce) DO NOTHING",
                (nonce, expires_at),
            )
            return cursor.rowcount == 1

    # -- maintenance ------------------------------------------------------ #

    def stuck_executions(self, older_than_seconds: int = 300) -> list[Proposal]:
        """Proposals stranded mid-execution, for an operator to inspect."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM proposals WHERE state = %s AND created_at < %s",
                (str(ProposalState.EXECUTING), cutoff),
            )
            return [self._proposal_from_row(row) for row in cursor.fetchall()]
