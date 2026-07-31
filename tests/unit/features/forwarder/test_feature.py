from datetime import UTC, datetime

from yukibot.contracts import (
    MessageRef as ContractMessageRef,
)
from yukibot.contracts import (
    TelegramContentType,
    TelegramMessage,
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
)
from yukibot.features.forwarder import (
    ForwarderFeature,
    ForwardingReport,
    SyncOperation,
    SyncReport,
)
from yukibot.kernel import InProcessEventBus


class FakeForwarder:
    def __init__(self) -> None:
        self.messages = []
        self.edits = []
        self.deletes = []
        self.closed = False

    async def handle_message(self, message):  # type: ignore[no-untyped-def]
        self.messages.append(message)
        return ForwardingReport()

    async def handle_edit(self, message):  # type: ignore[no-untyped-def]
        self.edits.append(message)
        return SyncReport(SyncOperation.EDIT)

    async def handle_delete(self, event):  # type: ignore[no-untyped-def]
        self.deletes.append(event)
        return SyncReport(SyncOperation.DELETE)

    async def close(self) -> None:
        self.closed = True


def telegram_message() -> TelegramMessage:
    return TelegramMessage(
        ContractMessageRef(-1001, 10),
        TelegramContentType.TEXT,
        datetime.now(UTC),
        sender_id=42,
        topic_id=7,
        text="hello",
    )


async def test_feature_maps_contract_events_and_unsubscribes_on_stop() -> None:
    bus = InProcessEventBus()
    forwarder = FakeForwarder()
    feature = ForwarderFeature(bus, forwarder)  # type: ignore[arg-type]
    await feature.start()
    await feature.start()

    message = telegram_message()
    assert (await bus.publish(TelegramMessageReceived(message))).handler_count == 1
    assert (await bus.publish(TelegramMessageEdited(message))).handler_count == 1
    assert (
        await bus.publish(TelegramMessagesDeleted((10,), datetime.now(UTC), chat_id=-1001))
    ).handler_count == 1

    assert forwarder.messages[0].ref.chat_id == -1001
    assert forwarder.messages[0].topic_id == 7
    assert forwarder.edits[0].text == "hello"
    assert forwarder.deletes[0].message_ids == (10,)

    await feature.stop()
    assert forwarder.closed
    assert (await bus.publish(TelegramMessageReceived(message))).handler_count == 0
