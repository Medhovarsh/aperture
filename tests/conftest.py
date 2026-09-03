"""Shared fixtures.

Every test runs against a freshly generated demo workspace in a temp directory, so
tests never share a lineage log and never depend on execution order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aperture.demo import build_demo_workspace
from aperture.plane import ContextPlane
from aperture.workspace import Workspace


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    """A generated demo workspace on disk."""
    return build_demo_workspace(tmp_path / "workspace")


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    """The loaded demo workspace."""
    return Workspace.load(workspace_root)


@pytest.fixture()
def plane(workspace: Workspace) -> ContextPlane:
    """A context plane over the demo workspace."""
    return ContextPlane(workspace)


@pytest.fixture()
def client(tmp_path: Path):
    """A playground client with its own session pool and a frozen rate-limit clock."""
    fastapi = pytest.importorskip("fastapi", reason="playground needs the [web] extra")
    from fastapi.testclient import TestClient

    from aperture import playground

    playground.sessions = playground.SessionPool(tmp_path / "sessions")
    playground.limiter = playground.RateLimiter(clock=lambda: 1000.0)
    return TestClient(playground.app)
