"""Aperture - a governed context plane for AI agents.

Agents read enterprise data through one chokepoint that knows meaning, permission,
freshness, and how to explain a refusal.
"""

from .catalog import Catalog
from .enforcement import Enforcer
from .identity import PrincipalRegistry
from .lineage import LineageLog
from .plane import ContextPlane
from .policy import Policy, Rule, Verdict
from .reasons import Reason
from .router import SemanticRouter
from .types import (
    Principal,
    Record,
    SearchRequest,
    SearchResponse,
    Sensitivity,
    Source,
)
from .workspace import Workspace, WorkspaceError

__version__ = "0.1.0"

__all__ = [
    "Catalog",
    "ContextPlane",
    "Enforcer",
    "LineageLog",
    "Policy",
    "Principal",
    "PrincipalRegistry",
    "Reason",
    "Record",
    "Rule",
    "SearchRequest",
    "SearchResponse",
    "SemanticRouter",
    "Sensitivity",
    "Source",
    "Verdict",
    "Workspace",
    "WorkspaceError",
    "__version__",
]
