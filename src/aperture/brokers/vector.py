"""Vector-index broker.

Reads a JSONL chunk store of the shape most RAG pipelines already produce:

```
{"id": "...", "text": "...", "embedding": [0.1, ...], "acl": ["eng"], "tenant": "acme"}
```

If chunks carry embeddings and the caller supplies a query vector, ranking is cosine
similarity in pure Python. When either is absent the broker falls back to BM25 and
says so in the record notes, rather than pretending a lexical score is a semantic
one. Being honest about which ranking ran matters here: the whole product claim is
that the plane never hides what it did.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

from ..text import BM25Index
from ..types import Record, Sensitivity, Source
from .base import Broker, BrokerError


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(a) != len(b):
        raise ValueError("vectors must be the same length")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorBroker(Broker):
    """Retrieval over a JSONL chunk store."""

    kind = "vector"

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self._cache: dict[str, tuple[float, list[Record], list[list[float]] | None]] = {}

    def _load(self, source: Source) -> tuple[list[Record], list[list[float]] | None]:
        path_value = source.config.get("path")
        if not path_value:
            raise BrokerError(f"source {source.id} has no 'path' configured")
        path = self.resolve_path(str(path_value))
        if not path.is_file():
            raise BrokerError(f"chunk store not found: {path_value}")

        signature = path.stat().st_mtime
        cached = self._cache.get(source.id)
        if cached and cached[0] == signature:
            return cached[1], cached[2]

        records: list[Record] = []
        embeddings: list[list[float]] = []
        has_embeddings = True

        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BrokerError(f"{path_value}:{line_number}: invalid JSON: {exc}") from exc

            sensitivity = payload.get("sensitivity")
            records.append(
                Record(
                    id=str(payload.get("id") or f"chunk-{line_number}"),
                    source_id=source.id,
                    title=str(payload.get("title", "")),
                    text=str(payload.get("text", "")),
                    tenant=payload.get("tenant"),
                    acl=payload.get("acl"),
                    sensitivity=Sensitivity(sensitivity) if sensitivity else None,
                    updated_at=payload.get("updated_at"),
                    fields={
                        k: v
                        for k, v in payload.items()
                        if k not in {"embedding", "text", "acl", "tenant"}
                    },
                )
            )
            vector = payload.get("embedding")
            if isinstance(vector, list) and vector:
                embeddings.append([float(x) for x in vector])
            else:
                has_embeddings = False

        vectors = embeddings if has_embeddings and embeddings else None
        self._cache[source.id] = (signature, records, vectors)
        return records, vectors

    def search(
        self,
        source: Source,
        question: str,
        limit: int,
        query_vector: Sequence[float] | None = None,
    ) -> list[Record]:
        """Rank chunks by cosine similarity when possible, else BM25."""
        records, vectors = self._load(source)
        if not records:
            return []

        if vectors is not None and query_vector is not None:
            scored = sorted(
                (
                    (record, cosine(query_vector, vector))
                    for record, vector in zip(records, vectors)
                ),
                key=lambda pair: -pair[1],
            )
            return [
                record.model_copy(
                    update={"score": round(score, 4), "fields": {**record.fields, "ranking": "cosine"}}
                )
                for record, score in scored[:limit]
                if score > 0
            ]

        index = BM25Index([r.id for r in records], [f"{r.title} {r.text}" for r in records])
        by_id = {record.id: record for record in records}
        return [
            by_id[doc_id].model_copy(
                update={
                    "score": round(score, 4),
                    "fields": {**by_id[doc_id].fields, "ranking": "bm25_fallback"},
                }
            )
            for doc_id, score in index.top(question, limit)
        ]
