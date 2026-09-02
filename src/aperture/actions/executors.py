"""Action executors.

An executor knows how to do one thing to one system, and how to undo it when that
is possible. It answers three questions:

* ``estimate`` - what would this touch? Runs against real state, changes nothing.
* ``execute`` - do it, and return a compensation record if it can be undone.
* ``compensate`` - undo it, using only what ``execute`` recorded.

Executors never make authorization decisions. They are the hands; policy is the
judgment. Keeping them ignorant of identity is what makes them safe to write.

The executors here operate on a local SQLite "operations" database so the whole
gateway is demonstrable offline. Replacing them with ones that call Stripe, Zendesk,
or an internal API changes nothing above this file.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import ActionSpec, BlastRadius


class ExecutorError(RuntimeError):
    """Raised when an action cannot be estimated or run."""


class Executor(ABC):
    """Adapter for one action."""

    #: Name referenced by ActionSpec.executor.
    name: str = ""

    #: Whether this executor implements a compensating operation.
    reversible: bool = False

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    @abstractmethod
    def estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        """Measure what this action would touch, without changing anything."""

    @abstractmethod
    def execute(
        self, spec: ActionSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Perform the action.

        Returns:
            A tuple of (result, compensation). ``compensation`` is None for actions
            that cannot be undone.
        """

    def compensate(self, spec: ActionSpec, compensation: dict[str, Any]) -> dict[str, Any]:
        """Undo a previous execution. Only called when compensation was recorded."""
        raise ExecutorError(f"{self.name} declares no compensating operation")


class SqliteExecutor(Executor):
    """Shared SQLite plumbing for the demo executors."""

    def _database(self, spec: ActionSpec) -> Path:
        configured = spec.config.get("database")
        if not configured:
            raise ExecutorError(f"action {spec.id} has no 'database' configured")
        path = (self.workspace_root / str(configured)).resolve()
        root = self.workspace_root
        if root != path and root not in path.parents:
            raise ExecutorError(f"action database escapes the workspace: {configured}")
        if not path.is_file():
            raise ExecutorError(f"action database not found: {configured}")
        return path

    def _connect(self, spec: ActionSpec) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database(spec))
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


class RefundExecutor(SqliteExecutor):
    """Issue a customer refund. Reversible by reversing the refund."""

    name = "refund"
    reversible = True

    def estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        customer = str(arguments["customer_id"])
        amount = float(arguments["amount"])
        with self._connect(spec) as connection:
            row = connection.execute(
                "SELECT customer_id, name, lifetime_value FROM customers WHERE customer_id = ?",
                (customer,),
            ).fetchone()
        if row is None:
            raise ExecutorError(f"unknown customer: {customer}")
        return BlastRadius(
            summary=f"Refund {amount:,.2f} USD to {row['name']}",
            affected=1,
            amount=amount,
            reversible=True,
            details={"customer_id": customer, "lifetime_value": row["lifetime_value"]},
        )

    def execute(
        self, spec: ActionSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        customer = str(arguments["customer_id"])
        amount = float(arguments["amount"])
        with self._connect(spec) as connection:
            cursor = connection.execute(
                "INSERT INTO refunds (customer_id, amount, status, created_at) VALUES (?,?,?,?)",
                (customer, amount, "issued", self._now()),
            )
            refund_id = cursor.lastrowid
            connection.commit()
        return (
            {"refund_id": refund_id, "customer_id": customer, "amount": amount},
            {"refund_id": refund_id},
        )

    def compensate(self, spec: ActionSpec, compensation: dict[str, Any]) -> dict[str, Any]:
        refund_id = compensation["refund_id"]
        with self._connect(spec) as connection:
            connection.execute(
                "UPDATE refunds SET status = ? WHERE id = ?", ("reversed", refund_id)
            )
            connection.commit()
        return {"refund_id": refund_id, "status": "reversed"}


class TicketExecutor(SqliteExecutor):
    """Close a support ticket. Reversible by restoring the previous status."""

    name = "ticket"
    reversible = True

    def estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        ticket_id = str(arguments["ticket_id"])
        with self._connect(spec) as connection:
            row = connection.execute(
                "SELECT ticket_id, subject, status FROM tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        if row is None:
            raise ExecutorError(f"unknown ticket: {ticket_id}")
        return BlastRadius(
            summary=f"Close ticket {ticket_id}: {row['subject']}",
            affected=1,
            reversible=True,
            details={"current_status": row["status"]},
        )

    def execute(
        self, spec: ActionSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        ticket_id = str(arguments["ticket_id"])
        with self._connect(spec) as connection:
            row = connection.execute(
                "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise ExecutorError(f"unknown ticket: {ticket_id}")
            previous = row["status"]
            connection.execute(
                "UPDATE tickets SET status = ? WHERE ticket_id = ?", ("closed", ticket_id)
            )
            connection.commit()
        return (
            {"ticket_id": ticket_id, "status": "closed"},
            {"ticket_id": ticket_id, "previous_status": previous},
        )

    def compensate(self, spec: ActionSpec, compensation: dict[str, Any]) -> dict[str, Any]:
        with self._connect(spec) as connection:
            connection.execute(
                "UPDATE tickets SET status = ? WHERE ticket_id = ?",
                (compensation["previous_status"], compensation["ticket_id"]),
            )
            connection.commit()
        return {
            "ticket_id": compensation["ticket_id"],
            "status": compensation["previous_status"],
        }


class MessageExecutor(SqliteExecutor):
    """Send a message outside the company. Irreversible on purpose.

    Once a message leaves, it has left. Modelling this as reversible because a row
    can be deleted from a local table would be a lie the whole gateway rests on.
    """

    name = "message"
    reversible = False

    def estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        recipient = str(arguments["to"])
        body = str(arguments.get("body", ""))
        return BlastRadius(
            summary=f"Send an external message to {recipient}",
            affected=1,
            external_recipients=(recipient,),
            reversible=False,
            details={"characters": len(body), "subject": arguments.get("subject", "")},
        )

    def execute(
        self, spec: ActionSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        with self._connect(spec) as connection:
            cursor = connection.execute(
                "INSERT INTO messages (recipient, subject, body, sent_at) VALUES (?,?,?,?)",
                (
                    str(arguments["to"]),
                    str(arguments.get("subject", "")),
                    str(arguments.get("body", "")),
                    self._now(),
                ),
            )
            connection.commit()
        return {"message_id": cursor.lastrowid, "recipient": arguments["to"]}, None


class AccountPurgeExecutor(SqliteExecutor):
    """Delete every account in a region. Irreversible, and the reason blast radius exists.

    The argument is one short string. The consequence is measured in rows, and the
    difference between those two facts is what a human reviewer needs to see.
    """

    name = "account_purge"
    reversible = False

    def estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        region = str(arguments["region"])
        with self._connect(spec) as connection:
            rows = connection.execute(
                "SELECT customer_id, name, lifetime_value FROM customers WHERE region = ?",
                (region,),
            ).fetchall()
        value = sum(float(row["lifetime_value"] or 0) for row in rows)
        return BlastRadius(
            summary=f"Permanently delete every customer account in region '{region}'",
            affected=len(rows),
            amount=value,
            reversible=False,
            details={"names": [row["name"] for row in rows][:10]},
        )

    def execute(
        self, spec: ActionSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        region = str(arguments["region"])
        with self._connect(spec) as connection:
            cursor = connection.execute("DELETE FROM customers WHERE region = ?", (region,))
            connection.commit()
        return {"region": region, "deleted": cursor.rowcount}, None


EXECUTOR_CLASSES: dict[str, type[Executor]] = {
    RefundExecutor.name: RefundExecutor,
    TicketExecutor.name: TicketExecutor,
    MessageExecutor.name: MessageExecutor,
    AccountPurgeExecutor.name: AccountPurgeExecutor,
}


def build_executors(workspace_root: Path) -> dict[str, Executor]:
    """Instantiate every known executor for a workspace."""
    return {name: cls(workspace_root) for name, cls in EXECUTOR_CLASSES.items()}
