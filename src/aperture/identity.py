"""Principal registry.

v1 resolves identity from a static file. The registry is deliberately behind a small
interface so a later release can back it with an IdP (Okta, Entra, SCIM) without
touching the policy engine or the pipeline.

Identity never comes from prompt text or from tool arguments an untrusted model can
influence, unless the workspace explicitly opts into caller-asserted identity for
local development.
"""

from __future__ import annotations

from typing import Any, Iterator

from .types import Principal


class PrincipalRegistry:
    """Lookup of principal id to :class:`Principal`."""

    def __init__(self, principals: list[Principal]) -> None:
        self._by_id: dict[str, Principal] = {}
        for principal in principals:
            if principal.id in self._by_id:
                raise ValueError(f"duplicate principal id: {principal.id}")
            self._by_id[principal.id] = principal

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrincipalRegistry":
        """Build a registry from parsed YAML."""
        entries = data.get("principals") or []
        if not isinstance(entries, list):
            raise ValueError("'principals' must be a list")
        return cls([Principal.model_validate(entry) for entry in entries])

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[Principal]:
        return iter(self._by_id.values())

    def get(self, principal_id: str) -> Principal | None:
        """Return a principal, or None when the identity is unregistered."""
        return self._by_id.get(principal_id)

    def ids(self) -> list[str]:
        """All registered principal ids."""
        return list(self._by_id)
