"""Semantic source routing.

Agents ask questions; they do not name sources. The router decides which registered
sources are worth querying by matching the question against each source's catalog
description - the "what questions this answers" text an owner writes when they
register it.

Two properties matter more than ranking quality:

* **Routing is explainable.** Every decision carries a score and is recorded in
  lineage, so "why did it not look at the finance warehouse?" has an answer.
* **Routing never silently returns nothing.** If no description matches, the router
  falls back to all eligible sources rather than producing an empty result that
  looks like an authorization outcome. Confusing "found nothing" with "not allowed"
  is exactly the failure mode this product exists to remove.
"""

from __future__ import annotations

from pydantic import BaseModel

from .text import BM25Index, join_for_index
from .types import Source


class RoutedSource(BaseModel):
    """A source the router selected, with its justification."""

    source_id: str
    score: float
    reason: str


class SemanticRouter:
    """Ranks candidate sources for a question."""

    def __init__(self, min_score: float = 0.0) -> None:
        self.min_score = min_score

    def route(
        self,
        question: str,
        eligible: list[Source],
        limit: int = 3,
    ) -> list[RoutedSource]:
        """Select up to ``limit`` sources to query, best first."""
        if not eligible:
            return []
        if len(eligible) == 1:
            return [
                RoutedSource(
                    source_id=eligible[0].id, score=1.0, reason="only eligible source"
                )
            ]

        index = BM25Index(
            [source.id for source in eligible],
            [
                join_for_index([source.title, source.description, " ".join(source.tags)])
                for source in eligible
            ],
        )
        ranked = [
            (source_id, score)
            for source_id, score in index.top(question, limit)
            if score > self.min_score
        ]

        if ranked:
            return [
                RoutedSource(
                    source_id=source_id,
                    score=round(score, 4),
                    reason="question matched source description",
                )
                for source_id, score in ranked
            ]

        return [
            RoutedSource(
                source_id=source.id,
                score=0.0,
                reason="no description matched; querying all eligible sources",
            )
            for source in eligible[:limit]
        ]
