"""Document-corpus broker.

Reads a directory of Markdown files whose YAML frontmatter carries the governance
metadata:

```
---
title: Parental Leave Policy
acl: [hr, people-managers]
tenant: acme
sensitivity: confidential
updated_at: 2026-08-01
tags: [policy, benefits]
---
```

Frontmatter is the stand-in for a real content management system's permission
model. Everything the pipeline needs - who may see it, which tenant owns it, when
it last changed - travels with the document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..text import BM25Index, join_for_index
from ..types import Record, Sensitivity, Source
from .base import Broker, BrokerError

_FRONTMATTER_DELIM = "---"


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a document into frontmatter mapping and body text."""
    if not raw.startswith(_FRONTMATTER_DELIM):
        return {}, raw
    parts = raw.split(_FRONTMATTER_DELIM, 2)
    if len(parts) < 3:
        return {}, raw
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise BrokerError(f"invalid frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        return {}, parts[2]
    return meta, parts[2].lstrip("\n")


def _coerce_timestamp(value: Any) -> datetime | None:
    """Accept dates, datetimes, or ISO strings from frontmatter."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    # PyYAML parses bare dates into datetime.date.
    if hasattr(value, "year") and hasattr(value, "month"):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


class DocsBroker(Broker):
    """Retrieval over a local Markdown corpus, ranked with BM25."""

    kind = "docs"

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self._cache: dict[str, tuple[float, list[Record], BM25Index]] = {}

    def _load(self, source: Source) -> tuple[list[Record], BM25Index]:
        """Load and index a corpus, reusing the cache while the directory is unchanged."""
        path_value = source.config.get("path")
        if not path_value:
            raise BrokerError(f"source {source.id} has no 'path' configured")
        directory = self.resolve_path(str(path_value))
        if not directory.is_dir():
            raise BrokerError(f"corpus directory not found: {path_value}")

        files = sorted(directory.rglob("*.md"))
        signature = max((f.stat().st_mtime for f in files), default=0.0) + len(files)
        cached = self._cache.get(source.id)
        if cached and cached[0] == signature:
            return cached[1], cached[2]

        records: list[Record] = []
        for path in files:
            meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
            record_id = str(meta.get("id") or path.relative_to(directory).as_posix())
            sensitivity = meta.get("sensitivity")
            records.append(
                Record(
                    id=record_id,
                    source_id=source.id,
                    title=str(meta.get("title") or path.stem.replace("-", " ").title()),
                    text=body.strip(),
                    tenant=meta.get("tenant"),
                    acl=meta.get("acl"),
                    sensitivity=Sensitivity(sensitivity) if sensitivity else None,
                    updated_at=_coerce_timestamp(meta.get("updated_at")),
                    fields={
                        "path": path.relative_to(self.workspace_root).as_posix(),
                        "tags": tuple(meta.get("tags") or ()),
                    },
                )
            )

        index = BM25Index(
            [r.id for r in records],
            [join_for_index([r.title, r.text]) for r in records],
        )
        self._cache[source.id] = (signature, records, index)
        return records, index

    def search(self, source: Source, question: str, limit: int) -> list[Record]:
        """Rank corpus documents against the question."""
        records, index = self._load(source)
        by_id = {record.id: record for record in records}
        results: list[Record] = []
        for doc_id, score in index.top(question, limit):
            record = by_id[doc_id].model_copy(update={"score": round(score, 4)})
            results.append(record)
        return results

    def fetch(self, source: Source, record_id: str) -> Record | None:
        """Return one document by its corpus id."""
        records, _ = self._load(source)
        for record in records:
            if record.id == record_id:
                return record
        return None
