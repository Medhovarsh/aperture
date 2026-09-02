"""SQLite broker.

Structured records are where field-level redaction matters: a support agent may
legitimately need an employee's manager and location while having no business
seeing salary or national ID. Every column lands in ``Record.fields`` so the
enforcement pipeline can redact precisely rather than dropping whole rows.

Table and column names come from the catalog, never from the agent, and are
validated against the live schema before being interpolated - the agent has no
path to influence the SQL text.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..text import BM25Index, join_for_index
from ..types import Record, Sensitivity, Source
from .base import Broker, BrokerError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_MAX_SCAN = 2000


def _check_identifier(value: str, what: str) -> str:
    """Reject anything that is not a bare SQL identifier."""
    if not _IDENTIFIER_RE.match(value):
        raise BrokerError(f"invalid {what} in catalog config: {value!r}")
    return value


class SqlBroker(Broker):
    """Retrieval over a SQLite table, ranked with BM25 over configured text columns."""

    kind = "sql"

    def _connect(self, source: Source) -> sqlite3.Connection:
        database = source.config.get("database")
        if not database:
            raise BrokerError(f"source {source.id} has no 'database' configured")
        path: Path = self.resolve_path(str(database))
        if not path.is_file():
            raise BrokerError(f"database not found: {database}")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _rows(self, source: Source) -> list[sqlite3.Row]:
        """Read the configured table, bounded by max_scan."""
        table = _check_identifier(str(source.config.get("table", "")), "table")
        max_scan = int(source.config.get("max_scan", _DEFAULT_MAX_SCAN))
        try:
            with self._connect(source) as connection:
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if not columns:
                    raise BrokerError(f"table not found: {table}")
                self._validate_columns(source, columns)
                return list(connection.execute(f"SELECT * FROM {table} LIMIT ?", (max_scan,)))
        except sqlite3.Error as exc:
            raise BrokerError(f"sqlite error on {source.id}: {exc}") from exc

    @staticmethod
    def _validate_columns(source: Source, columns: set[str]) -> None:
        """Fail loudly when the catalog references columns the table does not have."""
        configured: list[str] = []
        for key in ("id_column", "acl_column", "tenant_column", "updated_column"):
            value = source.config.get(key)
            if value:
                configured.append(str(value))
        configured.extend(str(c) for c in source.config.get("text_columns", ()) or ())
        missing = [name for name in configured if name not in columns]
        if missing:
            raise BrokerError(
                f"source {source.id} references missing columns: {', '.join(sorted(missing))}"
            )

    def _to_records(self, source: Source) -> list[Record]:
        config = source.config
        id_column = str(config.get("id_column", "id"))
        text_columns = [str(c) for c in config.get("text_columns", ()) or ()]
        acl_column = config.get("acl_column")
        tenant_column = config.get("tenant_column")
        updated_column = config.get("updated_column")
        sensitivity_column = config.get("sensitivity_column")

        records: list[Record] = []
        for row in self._rows(source):
            data: dict[str, Any] = dict(row)
            text = " ".join(str(data.get(column, "") or "") for column in text_columns)
            acl_raw = data.get(str(acl_column)) if acl_column else None
            acl = (
                tuple(part.strip() for part in str(acl_raw).split(",") if part.strip())
                if acl_raw
                else None
            )
            sensitivity_raw = data.get(str(sensitivity_column)) if sensitivity_column else None
            records.append(
                Record(
                    id=str(data.get(id_column)),
                    source_id=source.id,
                    title=str(data.get(text_columns[0], "")) if text_columns else "",
                    text=text.strip(),
                    tenant=str(data.get(str(tenant_column))) if tenant_column else None,
                    acl=acl,
                    sensitivity=Sensitivity(sensitivity_raw) if sensitivity_raw else None,
                    updated_at=data.get(str(updated_column)) if updated_column else None,
                    fields=data,
                )
            )
        return records

    def search(self, source: Source, question: str, limit: int) -> list[Record]:
        """Rank rows against the question."""
        records = self._to_records(source)
        if not records:
            return []
        index = BM25Index(
            [r.id for r in records],
            [join_for_index([r.title, r.text]) for r in records],
        )
        by_id = {record.id: record for record in records}
        return [
            by_id[doc_id].model_copy(update={"score": round(score, 4)})
            for doc_id, score in index.top(question, limit)
        ]
