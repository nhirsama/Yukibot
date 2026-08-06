"""Lifecycle and command registration for the independent summarizer module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from yukibot.kernel import CommandHandler, CommandRegistry, CommandSubscription

from .commands import SUMMARY_HELP


class SummarizerFeature:
    name = "summarizer"

    def __init__(
        self,
        command_registry: CommandRegistry,
        command_handler: CommandHandler,
        *,
        shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._commands = command_registry
        self._handler = command_handler
        self._shutdown = shutdown
        self._subscription: CommandSubscription | None = None

    async def start(self) -> None:
        if self._subscription is not None:
            return
        self._subscription = self._commands.register(
            "/summary",
            summary="生成并发送消息总结",
            help_text=SUMMARY_HELP,
            handler=self._handler,
        )

    async def stop(self) -> None:
        if self._subscription is None:
            return
        self._subscription.unregister()
        self._subscription = None
        if self._shutdown is not None:
            await self._shutdown()
