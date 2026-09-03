"""Workspace loading.

A workspace is a directory holding the three governance documents plus the data
they point at:

```
catalog.yaml       registered sources
policy.yaml        access rules
principals.yaml    identities
data/              corpora and databases referenced by the catalog
lineage/access.jsonl
```

Loading is strict by design. A malformed policy or catalog raises, and the server
refuses to start - an unparseable policy must never degrade into an open one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .actions.catalog import ActionCatalog
from .actions.gateway import ActionGateway
from .actions.store import ActionStore, open_store
from .catalog import Catalog
from .identity import PrincipalRegistry
from .lineage import LineageLog
from .policy import Policy

CATALOG_FILE = "catalog.yaml"
POLICY_FILE = "policy.yaml"
PRINCIPALS_FILE = "principals.yaml"
ACTIONS_FILE = "actions.yaml"
LINEAGE_FILE = "lineage/access.jsonl"
STATE_DIR = "state"


class WorkspaceError(RuntimeError):
    """Raised when a workspace is missing or invalid."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkspaceError(f"missing required file: {path.name} (looked in {path.parent})")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WorkspaceError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceError(f"{path.name} must contain a mapping at the top level")
    return data


class Workspace:
    """A loaded, validated governance configuration."""

    def __init__(
        self,
        root: Path,
        catalog: Catalog,
        policy: Policy,
        principals: PrincipalRegistry,
        actions: ActionCatalog | None = None,
    ) -> None:
        self.root = root
        self.catalog = catalog
        self.policy = policy
        self.principals = principals
        # Absent actions.yaml means the workspace is read-only, which is a valid and
        # deliberately common configuration.
        self.actions = actions or ActionCatalog.empty()

    @classmethod
    def load(cls, root: Path) -> "Workspace":
        """Load a workspace from disk, raising :class:`WorkspaceError` on any fault."""
        root = Path(root).resolve()
        if not root.is_dir():
            raise WorkspaceError(f"workspace directory not found: {root}")
        try:
            catalog = Catalog.from_dict(_load_yaml(root / CATALOG_FILE))
            policy = Policy.from_dict(_load_yaml(root / POLICY_FILE))
            principals = PrincipalRegistry.from_dict(_load_yaml(root / PRINCIPALS_FILE))
            actions_path = root / ACTIONS_FILE
            actions = (
                ActionCatalog.from_dict(_load_yaml(actions_path))
                if actions_path.is_file()
                else ActionCatalog.empty()
            )
        except WorkspaceError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a single startup error
            raise WorkspaceError(f"invalid workspace configuration: {exc}") from exc

        workspace = cls(
            root=root, catalog=catalog, policy=policy,
            principals=principals, actions=actions,
        )
        workspace.check_references()
        return workspace

    def check_references(self) -> None:
        """Verify that policy rules only reference sources that exist.

        A rule pointing at a deleted source is nearly always a governance mistake,
        and silently ignoring it is how permissions rot.
        """
        known = set(self.catalog.ids())
        known_actions = set(self.actions.ids())
        for rule in self.policy.rules:
            unknown = [s for s in rule.when.sources if s not in known]
            if unknown:
                raise WorkspaceError(
                    f"policy rule '{rule.id}' references unregistered source(s): "
                    f"{', '.join(sorted(unknown))}"
                )
            unknown_actions = [
                a for a in rule.when.actions if a != "*" and a not in known_actions
            ]
            if unknown_actions:
                raise WorkspaceError(
                    f"policy rule '{rule.id}' references unregistered action(s): "
                    f"{', '.join(sorted(unknown_actions))}"
                )

    @property
    def lineage(self) -> LineageLog:
        """The access lineage log for this workspace.

        Reads and actions share one chain, so an auditor reconstructing an incident
        sees what the agent looked at and what it then did, in order.
        """
        return LineageLog(self.root / LINEAGE_FILE)

    @property
    def action_store(self) -> ActionStore:
        """Durable proposal and execution state.

        SQLite by default. Setting APERTURE_STORE_DSN switches the deployment to
        Postgres, which is what a fleet of workers needs: the execution claim only
        admits one winner if every worker shares one consistency domain.
        """
        return open_store(self.root / STATE_DIR)

    def gateway(self) -> ActionGateway:
        """Build the action gateway for this workspace."""
        return ActionGateway(
            principals=self.principals,
            policy=self.policy,
            catalog=self.actions,
            store=self.action_store,
            lineage=self.lineage,
            workspace_root=self.root,
        )
