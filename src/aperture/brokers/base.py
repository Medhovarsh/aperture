"""Broker interface.

A broker turns a registered :class:`~aperture.types.Source` plus a question into
candidate :class:`~aperture.types.Record` objects. Brokers do no authorization -
that is the enforcement pipeline's job - but they are responsible for faithfully
carrying the source system's metadata (ACL, tenant, timestamp) onto every record.

A broker that cannot determine a record's ACL must leave it as ``None``. The
pipeline treats that as most-restrictive, so guessing is always worse than
admitting ignorance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..types import Record, Source


class BrokerError(RuntimeError):
    """Raised when a source cannot be queried.

    The pipeline catches this and reports the source as unavailable rather than
    silently returning fewer results.
    """


class Broker(ABC):
    """Adapter for one kind of data source."""

    #: The Source.kind value this broker handles.
    kind: str = ""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def resolve_path(self, value: str) -> Path:
        """Resolve a source-configured path inside the workspace.

        Paths are confined to the workspace root: a catalog entry cannot be used to
        read arbitrary files off the host.
        """
        candidate = (self.workspace_root / value).resolve()
        root = self.workspace_root.resolve()
        if root != candidate and root not in candidate.parents:
            raise BrokerError(f"source path escapes the workspace: {value}")
        return candidate

    @abstractmethod
    def search(self, source: Source, question: str, limit: int) -> list[Record]:
        """Return up to ``limit`` candidate records, best first.

        Raises:
            BrokerError: when the source is unreachable or misconfigured.
        """

    def fetch(self, source: Source, record_id: str) -> Record | None:
        """Return a single record by id, or None when it does not exist.

        The default implementation is a linear scan over a wide search, which is
        correct but slow; real adapters should override it.
        """
        for record in self.search(source, record_id, limit=200):
            if record.id == record_id:
                return record
        return None
