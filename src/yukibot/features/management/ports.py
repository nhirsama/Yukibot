"""Ports owned by the management feature."""

from __future__ import annotations

from typing import Protocol


class OwnerIdentity(Protocol):
    @property
    def user_id(self) -> int: ...


class AdministrationRepository(Protocol):
    async def is_admin(self, user_id: int) -> bool: ...

    async def list_admins(self) -> tuple[int, ...]: ...

    async def add_admin(self, user_id: int, *, granted_by: int) -> None: ...

    async def remove_admin(self, user_id: int) -> None: ...
