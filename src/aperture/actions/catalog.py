"""The action registry.

Mirrors the source catalog: an action that is not registered here cannot be
proposed, so granting an agent a new power is a governance change rather than a
code change.
"""

from __future__ import annotations

from typing import Any, Iterator

from .executors import EXECUTOR_CLASSES
from .types import ActionSpec


class ActionCatalogError(ValueError):
    """Raised when the action catalog is structurally invalid."""


class ActionCatalog:
    """An immutable, validated collection of registered actions."""

    def __init__(self, actions: list[ActionSpec]) -> None:
        self._by_id: dict[str, ActionSpec] = {}
        for action in actions:
            if action.id in self._by_id:
                raise ActionCatalogError(f"duplicate action id: {action.id}")
            self._by_id[action.id] = action
        self._validate()

    def _validate(self) -> None:
        """Reject catalogs that would mislead a reviewer.

        The reversibility check is the important one. An action advertised as
        reversible whose executor cannot undo it would show a human reviewer
        "reversible" on the approval screen and then be unable to honor it.
        """
        for action in self._by_id.values():
            executor_class = EXECUTOR_CLASSES.get(action.executor)
            if executor_class is None:
                raise ActionCatalogError(
                    f"action '{action.id}' names unknown executor '{action.executor}'"
                )
            if action.reversible and not executor_class.reversible:
                raise ActionCatalogError(
                    f"action '{action.id}' is declared reversible but executor "
                    f"'{action.executor}' implements no compensating operation"
                )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionCatalog":
        """Build a catalog from parsed YAML. An absent 'actions' key yields an empty one."""
        entries = data.get("actions") or []
        if not isinstance(entries, list):
            raise ActionCatalogError("'actions' must be a list")
        return cls([ActionSpec.model_validate(entry) for entry in entries])

    @classmethod
    def empty(cls) -> "ActionCatalog":
        """A catalog with no actions: the plane stays read-only."""
        return cls([])

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[ActionSpec]:
        return iter(self._by_id.values())

    def __contains__(self, action_id: object) -> bool:
        return action_id in self._by_id

    def get(self, action_id: str) -> ActionSpec | None:
        """Return an action by id, or None when it is not registered."""
        return self._by_id.get(action_id)

    def ids(self) -> list[str]:
        """All registered action ids."""
        return list(self._by_id)
