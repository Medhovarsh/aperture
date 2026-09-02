"""Broker registry.

Adding a connector means adding a class here. Nothing else in the plane changes,
which is the point of keeping authorization out of brokers entirely.
"""

from __future__ import annotations

from pathlib import Path

from .base import Broker, BrokerError
from .docs import DocsBroker
from .sql import SqlBroker
from .vector import VectorBroker

BROKER_CLASSES: dict[str, type[Broker]] = {
    DocsBroker.kind: DocsBroker,
    SqlBroker.kind: SqlBroker,
    VectorBroker.kind: VectorBroker,
}


def build_brokers(workspace_root: Path) -> dict[str, Broker]:
    """Instantiate one broker per supported source kind."""
    return {kind: cls(workspace_root) for kind, cls in BROKER_CLASSES.items()}


__all__ = [
    "Broker",
    "BrokerError",
    "DocsBroker",
    "SqlBroker",
    "VectorBroker",
    "BROKER_CLASSES",
    "build_brokers",
]
