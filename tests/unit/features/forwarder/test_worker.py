from datetime import UTC, datetime

from yukibot.contracts import (
    MessageRef,
    TelegramContentType,
    TelegramMessage,
    TelegramMessageEdited,
    TelegramMessageReceived,
)
from yukibot.features.forwarder import PermanentDeliveryError, RetryAfter
from yukibot.features.forwarder.jobs import ForwardJob, ForwardJobKind
from yukibot.features.forwarder.service import (
    DeliveryFailure,
    ForwardingReport,
    SyncOperation,
    SyncReport,
)
from yukibot.features.forwarder.worker import ForwardJobProcessor, ForwardJobRunner


def received_job(job_id: int, message_id: int, *, attempts: int = 1) -> ForwardJob:
    message = TelegramMessage(
        MessageRef(-1001, message_id),
        TelegramContentType.TEXT,
        datetime.now(UTC),
        text="hello",
    )
    return ForwardJob(
        job_id,
        ForwardJobKind.RECEIVE,
        TelegramMessageReceived(message),
        attempts,
    )


class FakeRepository:
    def __init__(self, jobs: tuple[ForwardJob, ...]) -> None:
        self.jobs = jobs
        self.succeeded: tuple[int, ...] = ()
        self.failed: tuple[tuple[int, ...], str] | None = None
        self.rescheduled: tuple[tuple[int, ...], float, str] | None = None

    async def claim_due(self, now: float):  # type: ignore[no-untyped-def]
        jobs, self.jobs = self.jobs, ()
        return jobs

    async def mark_succeeded(self, job_ids):  # type: ignore[no-untyped-def]
        self.succeeded = tuple(job_ids)

    async def mark_failed(self, job_ids, error):  # type: ignore[no-untyped-def]
        self.failed = (tuple(job_ids), error)

    async def reschedule(self, job_ids, *, available_at, error):  # type: ignore[no-untyped-def]
        self.rescheduled = (tuple(job_ids), available_at, error)


class StubProcessor:
    def __init__(self, result: ForwardingReport | Exception) -> None:
        self.result = result

    async def execute(self, jobs):  # type: ignore[no-untyped-def]
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_runner_marks_successful_batch_complete() -> None:
    repository = FakeRepository((received_job(1, 10), received_job(2, 11)))
    runner = ForwardJobRunner(
        repository,  # type: ignore[arg-type]
        StubProcessor(ForwardingReport()),  # type: ignore[arg-type]
        clock=lambda: 100.0,
    )

    assert await runner.process_once()
    assert repository.succeeded == (1, 2)
    assert repository.failed is None
    assert repository.rescheduled is None


async def test_runner_honors_retry_after() -> None:
    repository = FakeRepository((received_job(1, 10, attempts=2),))
    runner = ForwardJobRunner(
        repository,  # type: ignore[arg-type]
        StubProcessor(RetryAfter(120)),  # type: ignore[arg-type]
        clock=lambda: 100.0,
    )

    await runner.process_once()

    assert repository.rescheduled is not None
    assert repository.rescheduled[:2] == ((1,), 220.0)
    assert "RetryAfter" in repository.rescheduled[2]


async def test_runner_fails_permanent_errors_without_retry() -> None:
    repository = FakeRepository((received_job(1, 10),))
    runner = ForwardJobRunner(
        repository,  # type: ignore[arg-type]
        StubProcessor(PermanentDeliveryError("forbidden")),  # type: ignore[arg-type]
        clock=lambda: 100.0,
    )

    await runner.process_once()

    assert repository.failed is not None
    assert repository.failed[0] == (1,)
    assert repository.rescheduled is None


async def test_runner_retries_mixed_route_failures() -> None:
    job = received_job(1, 10)
    source = job.event.message.ref  # type: ignore[union-attr]
    report = ForwardingReport(
        failures=(
            DeliveryFailure(1, (source,), PermanentDeliveryError("forbidden")),
            DeliveryFailure(2, (source,), RetryAfter(5)),
        )
    )
    repository = FakeRepository((job,))
    runner = ForwardJobRunner(
        repository,  # type: ignore[arg-type]
        StubProcessor(report),  # type: ignore[arg-type]
        clock=lambda: 100.0,
    )

    await runner.process_once()

    assert repository.rescheduled is not None
    assert repository.rescheduled[:2] == ((1,), 105.0)
    assert repository.failed is None


class FakeService:
    def __init__(self) -> None:
        self.album_ids: tuple[int, ...] = ()
        self.edited_id: int | None = None

    async def forward_album(self, messages):  # type: ignore[no-untyped-def]
        self.album_ids = tuple(message.ref.message_id for message in messages)
        return ForwardingReport()

    async def forward_message(self, message):  # type: ignore[no-untyped-def]
        return ForwardingReport()

    async def synchronize_edit(self, message):  # type: ignore[no-untyped-def]
        self.edited_id = message.ref.message_id
        return SyncReport(SyncOperation.EDIT)


async def test_processor_dispatches_album_and_edit_jobs() -> None:
    service = FakeService()
    processor = ForwardJobProcessor(service)  # type: ignore[arg-type]
    await processor.execute((received_job(1, 10), received_job(2, 11)))

    edit_message = received_job(3, 12).event.message  # type: ignore[union-attr]
    edit = ForwardJob(
        3,
        ForwardJobKind.EDIT,
        TelegramMessageEdited(edit_message),
        1,
    )
    await processor.execute((edit,))

    assert service.album_ids == (10, 11)
    assert service.edited_id == 12
