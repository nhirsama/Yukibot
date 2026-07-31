"""Ports required by the forwarding application service."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import (
    DestinationEndpoint,
    ForwardMode,
    IncomingMessage,
    MessageLink,
    MessageRef,
    Route,
)


class RouteRepository(Protocol):
    async def list_for_source_chat(self, chat_id: int) -> Sequence[Route]: ...

    async def list_all(self) -> Sequence[Route]: ...

    async def add(self, route: Route) -> None: ...

    async def replace(self, route: Route) -> None: ...

    async def remove(self, route_id: int) -> bool: ...


class MessageLinkRepository(Protocol):
    async def save_many(self, links: Sequence[MessageLink]) -> None: ...

    async def get(self, route_id: int, source: MessageRef) -> MessageLink | None: ...

    async def find_all(self, source: MessageRef) -> Sequence[MessageLink]: ...

    async def find_by_source_message_id(self, message_id: int) -> Sequence[MessageLink]: ...

    async def remove(self, link: MessageLink) -> None: ...


class TelegramGateway(Protocol):
    async def deliver_message(
        self,
        message: IncomingMessage,
        destination: DestinationEndpoint,
        *,
        mode: ForwardMode,
        reply_to_message_id: int | None,
    ) -> MessageRef: ...

    async def deliver_album(
        self,
        messages: Sequence[IncomingMessage],
        destination: DestinationEndpoint,
        *,
        mode: ForwardMode,
        reply_to_message_id: int | None,
    ) -> Sequence[MessageRef]: ...

    async def send_text(
        self,
        text: str,
        destination: DestinationEndpoint,
        *,
        reply_to_message_id: int | None,
    ) -> MessageRef: ...

    async def edit_from_source(self, source: IncomingMessage, target: MessageRef) -> None: ...

    async def delete_message(self, target: MessageRef) -> None: ...
