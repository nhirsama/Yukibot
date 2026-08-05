"""Bot-wide administration use cases and command authorization."""

from __future__ import annotations

from dataclasses import dataclass

from yukibot.kernel import ControlCommand, ModuleController, ModuleStatus

from .ports import AdministrationRepository, OwnerIdentity


@dataclass(slots=True)
class ManagementService:
    admins: AdministrationRepository
    modules: ModuleController
    owner: OwnerIdentity

    async def is_authorized(self, command: ControlCommand) -> bool:
        if command.outgoing:
            return True
        return command.actor_id is not None and await self.admins.is_admin(command.actor_id)

    async def list_admins(self) -> tuple[int, tuple[int, ...]]:
        delegated = tuple(
            user_id for user_id in await self.admins.list_admins() if user_id != self.owner.user_id
        )
        return self.owner.user_id, delegated

    async def add_admin(self, command: ControlCommand, user_id: int) -> None:
        await self._require_admin(command)
        if user_id <= 0:
            raise ValueError("administrator user ID must be positive")
        if user_id == self.owner.user_id:
            return
        granted_by = self.owner.user_id if command.outgoing else command.actor_id
        if granted_by is None:
            raise PermissionError("administrator permission is required")
        await self.admins.add_admin(user_id, granted_by=granted_by)

    async def remove_admin(self, command: ControlCommand, user_id: int) -> None:
        await self._require_admin(command)
        if user_id <= 0:
            raise ValueError("administrator user ID must be positive")
        if user_id == self.owner.user_id:
            raise ValueError("the current account owner cannot be removed")
        await self.admins.remove_admin(user_id)

    async def list_modules(self) -> tuple[ModuleStatus, ...]:
        return await self.modules.list_modules()

    async def enable_module(self, name: str) -> ModuleStatus:
        return await self.modules.enable(name)

    async def disable_module(self, name: str) -> ModuleStatus:
        return await self.modules.disable(name)

    async def _require_admin(self, command: ControlCommand) -> None:
        if not await self.is_authorized(command):
            raise PermissionError("administrator permission is required")
