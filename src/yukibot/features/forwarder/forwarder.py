"""Convenience facade that adds album assembly to ForwarderService."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .album import AlbumBuffer, TaskFactory
from .models import IncomingMessage, MessagesDeleted
from .service import ForwarderService, ForwardingReport, SyncReport

BackgroundReportHandler = Callable[[ForwardingReport], Awaitable[None]]


class Forwarder:
    def __init__(
        self,
        service: ForwarderService,
        *,
        album_flush_delay: float = 0.8,
        on_background_report: BackgroundReportHandler | None = None,
        on_background_error: Callable[[BaseException], None] | None = None,
        task_factory: TaskFactory | None = None,
    ) -> None:
        self._service = service
        self._on_background_report = on_background_report
        self._albums = AlbumBuffer[tuple[int, int | str], IncomingMessage](
            self._flush_album,
            flush_delay=album_flush_delay,
            sort_key=lambda message: message.ref.message_id,
            on_error=on_background_error,
            task_factory=task_factory,
        )

    async def handle_message(self, message: IncomingMessage) -> ForwardingReport:
        if message.grouped_id is None:
            return await self._service.forward_message(message)
        key = (message.ref.chat_id, message.grouped_id)
        await self._albums.add(key, message)
        return ForwardingReport(buffered=True)

    async def handle_edit(self, message: IncomingMessage) -> SyncReport:
        return await self._service.synchronize_edit(message)

    async def handle_delete(self, event: MessagesDeleted) -> SyncReport:
        return await self._service.synchronize_delete(event)

    async def close(self, *, flush_albums: bool = True) -> None:
        await self._albums.close(flush=flush_albums)

    async def _flush_album(self, messages: tuple[IncomingMessage, ...]) -> None:
        report = await self._service.forward_album(messages)
        if self._on_background_report is not None:
            await self._on_background_report(report)
