"""Framework integration layer for the reusable forwarder core."""

from __future__ import annotations

import logging

from yukibot.contracts import (
    TelegramMessage,
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
)
from yukibot.kernel import EventBus, Subscription

from .forwarder import Forwarder
from .models import (
    ContentType,
    IncomingMessage,
    MessageRef,
    MessagesDeleted,
    ServiceKind,
    ServiceMessage,
)
from .service import ForwardingReport, SyncReport


class ForwarderFeature:
    name = "forwarder"

    def __init__(
        self,
        bus: EventBus,
        forwarder: Forwarder,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._bus = bus
        self._forwarder = forwarder
        self._subscriptions: list[Subscription] = []
        self._logger = logger or logging.getLogger(__name__)

    async def start(self) -> None:
        if self._subscriptions:
            return
        self._subscriptions.extend(
            (
                self._bus.subscribe(TelegramMessageReceived, self._on_message),
                self._bus.subscribe(TelegramMessageEdited, self._on_edit),
                self._bus.subscribe(TelegramMessagesDeleted, self._on_delete),
            )
        )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
        await self._forwarder.close()

    async def _on_message(self, event: TelegramMessageReceived) -> None:
        report = await self._forwarder.handle_message(_to_incoming(event.message))
        self._log_forward_report(report)

    async def _on_edit(self, event: TelegramMessageEdited) -> None:
        report = await self._forwarder.handle_edit(_to_incoming(event.message))
        self._log_sync_report(report)

    async def _on_delete(self, event: TelegramMessagesDeleted) -> None:
        report = await self._forwarder.handle_delete(
            MessagesDeleted(event.message_ids, chat_id=event.chat_id)
        )
        self._log_sync_report(report)

    async def log_background_report(self, report: ForwardingReport) -> None:
        self._log_forward_report(report)

    def log_background_error(self, error: BaseException) -> None:
        self._logger.error(
            "forwarder album failed",
            extra={"feature": self.name, "error_type": type(error).__name__},
            exc_info=error,
        )

    def _log_forward_report(self, report: ForwardingReport) -> None:
        level = logging.ERROR if report.failures else logging.INFO
        self._logger.log(
            level,
            "forwarding completed",
            extra={
                "feature": self.name,
                "matched_routes": report.matched_routes,
                "delivered_messages": report.delivered_messages,
                "failure_count": len(report.failures),
                "ignored_reason": report.ignored_reason,
                "buffered": report.buffered,
            },
        )

    def _log_sync_report(self, report: SyncReport) -> None:
        level = logging.ERROR if report.failures else logging.INFO
        self._logger.log(
            level,
            "forwarder synchronization completed",
            extra={
                "feature": self.name,
                "operation": report.operation.value,
                "synchronized": report.synchronized,
                "failure_count": len(report.failures),
                "ignored_reason": report.ignored_reason,
            },
        )


def _to_incoming(message: TelegramMessage) -> IncomingMessage:
    service = None
    if message.service is not None:
        service = ServiceMessage(
            kind=ServiceKind(message.service.kind.value),
            actor_name=message.service.actor_name,
            member_names=message.service.member_names,
            new_title=message.service.new_title,
        )
    return IncomingMessage(
        ref=MessageRef(message.ref.chat_id, message.ref.message_id),
        content_type=ContentType(message.content_type.value),
        occurred_at=message.occurred_at,
        topic_id=message.topic_id,
        sender_id=message.sender_id,
        text=message.text,
        caption=message.caption,
        reply_to_message_id=message.reply_to_message_id,
        media_group_id=message.grouped_id,
        service=service,
        outgoing=message.outgoing,
    )
