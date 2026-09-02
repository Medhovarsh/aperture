"""Lexical retrieval primitives.

Aperture is a governance layer, not a retrieval research project, so v1 ships a
dependency-free BM25 index. It runs anywhere with no model download, no API key,
and no vector database - which matters because the plane must be installable inside
a locked-down enterprise network.

The :class:`BM25Index` interface is intentionally narrow so a production deployment
can swap in a real embedding index without any other module noticing.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Small, uncontroversial stop list. Kept short on purpose: aggressive stopping hurts
# short enterprise queries more than it helps.
_STOPWORDS = frozenset(
    """a an the of and or to in for on with is are was were be been being as at by
    from that this these those it its our your their what which who whom how do does
    did can could should would may might will shall i we you they he she""".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, and drop stopwords."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class BM25Index:
    """Okapi BM25 over a fixed set of documents.

    Parameters follow the standard defaults (k1=1.5, b=0.75), which behave well on
    short-to-medium enterprise documents.
    """

    def __init__(
        self,
        doc_ids: Sequence[str],
        texts: Sequence[str],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if len(doc_ids) != len(texts):
            raise ValueError("doc_ids and texts must be the same length")
        self.k1 = k1
        self.b = b
        self.doc_ids = list(doc_ids)
        self._tokens: list[list[str]] = [tokenize(t) for t in texts]
        self._freqs: list[Counter[str]] = [Counter(toks) for toks in self._tokens]
        self._lengths: list[int] = [len(toks) for toks in self._tokens]
        self._avg_len: float = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

        df: Counter[str] = Counter()
        for freq in self._freqs:
            df.update(freq.keys())
        n = max(len(self._tokens), 1)
        # BM25+ style idf floor keeps common terms from going negative.
        self._idf: dict[str, float] = {
            term: math.log(1.0 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()
        }

    def __len__(self) -> int:
        return len(self.doc_ids)

    def score(self, query: str) -> list[tuple[str, float]]:
        """Score every document against the query, best first.

        Documents with a zero score are omitted; a query that matches nothing yields
        an empty list rather than an arbitrary ordering.
        """
        terms = tokenize(query)
        if not terms or not self.doc_ids:
            return []

        results: list[tuple[str, float]] = []
        for index, doc_id in enumerate(self.doc_ids):
            freq = self._freqs[index]
            length = self._lengths[index] or 1
            total = 0.0
            for term in terms:
                occurrences = freq.get(term)
                if not occurrences:
                    continue
                idf = self._idf.get(term, 0.0)
                denominator = occurrences + self.k1 * (
                    1 - self.b + self.b * length / (self._avg_len or 1.0)
                )
                total += idf * (occurrences * (self.k1 + 1)) / denominator
            if total > 0:
                results.append((doc_id, total))

        results.sort(key=lambda pair: (-pair[1], pair[0]))
        return results

    def top(self, query: str, limit: int) -> list[tuple[str, float]]:
        """Return at most ``limit`` scored documents."""
        return self.score(query)[:limit]


def estimate_tokens(text: str) -> int:
    """Rough token count used for budgeting.

    Deliberately provider-agnostic: about four characters per token, which is close
    enough for a budget gate and avoids a tokenizer dependency.
    """
    return max(1, len(text) // 4)


def join_for_index(parts: Iterable[str]) -> str:
    """Concatenate fields into a single indexable string."""
    return " \n".join(part for part in parts if part)
