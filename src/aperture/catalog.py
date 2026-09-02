"""The source registry.

The catalog is the only namespace agents can reach. A source that is not registered
here does not exist as far as the plane is concerned, which is what makes "add a
data source" a governance action rather than a code change.
"""

from __future__ import annotations

from typing import Any, Iterator

from .types import Source


class Catalog:
    """An immutable, validated collection of registered sources."""

    def __init__(self, sources: list[Source]) -> None:
        self._by_id: dict[str, Source] = {}
        for source in sources:
            if source.id in self._by_id:
                raise ValueError(f"duplicate source id in catalog: {source.id}")
            self._by_id[source.id] = source

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Catalog":
        """Build a catalog from parsed YAML.

        Raises on malformed entries. A broken catalog must stop startup rather than
        silently register a subset of sources.
        """
        entries = data.get("sources") or []
        if not isinstance(entries, list):
            raise ValueError("catalog 'sources' must be a list")
        return cls([Source.model_validate(entry) for entry in entries])

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[Source]:
        return iter(self._by_id.values())

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._by_id

    def get(self, source_id: str) -> Source | None:
        """Return a source by id, or None when it is not registered."""
        return self._by_id.get(source_id)

    def require(self, source_id: str) -> Source:
        """Return a source by id, raising KeyError when unregistered."""
        source = self._by_id.get(source_id)
        if source is None:
            raise KeyError(f"unregistered source: {source_id}")
        return source

    def ids(self) -> list[str]:
        """All registered source ids, in registration order."""
        return list(self._by_id)
