import asyncio
from datetime import UTC, datetime

from yukibot.contracts import (
    MessageRef,
    TelegramContentType,
    TelegramMessage,
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
)
from yukibot.features.forwarder.feature import ForwarderFeature
from yukibot.features.forwarder.jobs import PendingForwardJob
from yukibot.kernel import InProcessEventBus, TaskSupervisor


class FakeRunner:
    def __init__(self) -> None:
        self.recovered = 2
        self.wakes = 0
        self.stopped = asyncio.Event()
        self.enqueued: list[PendingForwardJob] = []

    async def prepare(self) -> int:
        return self.recovered

    def wake(self) -> None:
        self.wakes += 1

    async def enqueue(self, jobs):  # type: ignore[no-untyped-def]
        self.enqueued.extend(jobs)
        self.wake()
        return len(jobs)

    def request_stop(self) -> None:
        self.stopped.set()

    async def run(self) -> None:
        await self.stopped.wait()


def telegram_message(*, grouped_id: int | None = None) -> TelegramMessage:
    now = datetime.now(UTC)
    return TelegramMessage(
        MessageRef(-1001, 10),
        TelegramContentType.TEXT,
        now,
        sender_id=42,
        topic_id=7,
        grouped_id=grouped_id,
        text="hello",
        edited_at=now,
    )


async def test_feature_persists_contract_events_and_unsubscribes_on_stop() -> None:
    bus = InProcessEventBus()
    runner = FakeRunner()
    supervisor = TaskSupervisor()
    feature = ForwarderFeature(
        bus,
        runner,  # type: ignore[arg-type]
        supervisor,
        album_delay=0.5,
        clock=lambda: 100.0,
    )
    await feature.start()
    await feature.start()

    message = telegram_message(grouped_id=50)
    received = TelegramMessageReceived(message)
    edited = TelegramMessageEdited(message)
    deleted = TelegramMessagesDeleted((10, 11), datetime.now(UTC), chat_id=-1001)
    assert (await bus.publish(received)).handler_count == 1
    assert (await bus.publish(edited)).handler_count == 1
    assert (await bus.publish(deleted)).handler_count == 1

    assert len(runner.enqueued) == 4
    assert runner.enqueued[0].event is received
    assert runner.enqueued[0].group_key == "album:-1001:50"
    assert runner.enqueued[0].available_at == 100.5
    assert runner.enqueued[1].event is edited
    assert [job.deduplication_key for job in runner.enqueued[2:]] == [
        "delete:-1001:10",
        "delete:-1001:11",
    ]
    assert runner.wakes == 4

    await feature.stop()
    assert runner.stopped.is_set()
    assert supervisor.active_count == 0
    assert (await bus.publish(received)).handler_count == 0
