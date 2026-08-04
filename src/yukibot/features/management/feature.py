"""Framework integration for bot-wide management commands."""

from __future__ import annotations

from yukibot.kernel import CommandRegistry, CommandSubscription

from .commands import ADMIN_HELP, ManagementCommands


class ManagementFeature:
    name = "management"

    def __init__(self, registry: CommandRegistry, commands: ManagementCommands) -> None:
        self._registry = registry
        self._commands = commands
        self._subscription: CommandSubscription | None = None

    async def start(self) -> None:
        if self._subscription is not None:
            return
        self._subscription = self._registry.register(
            "/admin",
            summary="管理管理员和功能模块",
            help_text=ADMIN_HELP,
            handler=self._commands.handle,
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.unregister()
            self._subscription = None
