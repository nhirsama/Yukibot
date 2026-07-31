"""Portable process shutdown coordination."""

from __future__ import annotations

import asyncio
import signal


class ShutdownCoordinator:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None
        self._installed: list[signal.Signals] = []

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self, reason: str = "requested") -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    async def wait(self) -> str:
        await self._event.wait()
        return self._reason or "requested"

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request, signum.name)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            self._installed.append(signum)

    def remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in self._installed:
            loop.remove_signal_handler(signum)
        self._installed.clear()
