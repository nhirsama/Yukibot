"""Lifecycle contract implemented by every feature and external resource."""

from typing import Protocol


class Feature(Protocol):
    @property
    def name(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
