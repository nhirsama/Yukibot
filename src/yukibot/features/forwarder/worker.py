"""Sequential durable-job execution for the forwarder feature."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from yukibot.contracts import (
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
)

from .errors import (
    DeliveryResultMismatch,
    MessageNotFound,
    NativeForwardUnsupported,
    PartialDeliveryState,
    PermanentDeliveryError,
    RetryAfter,
)
from .jobs import ForwardJob, ForwardJobKind, PendingForwardJob
from .models import IncomingMessage
from .ports import ForwardJobRepository
from .service import ForwarderService, ForwardingReport, SyncReport

type ProcessingReport = ForwardingReport | SyncReport


class ForwardJobProcessor:
    def __init__(self, service: ForwarderService) -> None:
        self._service = service

    async def execute(self, jobs: Sequence[ForwardJob]) -> ProcessingReport:
        if not jobs:
            raise ValueError("at least one forwarding job is required")
        kind = jobs[0].kind
        if any(job.kind is not kind for job in jobs):
            raise ValueError("a claimed job batch must contain one operation kind")

        if kind is ForwardJobKind.RECEIVE:
            messages = tuple(_received_message(job) for job in jobs)
            if len(messages) == 1:
                return await self._service.forward_message(messages[0])
            return await self._service.forward_album(messages)
        if len(jobs) != 1:
            raise ValueError(f"{kind.value} jobs cannot be processed as a batch")
        if kind is ForwardJobKind.EDIT:
            event = jobs[0].event
            if not isinstance(event, TelegramMessageEdited):
                raise TypeError("edit job contains the wrong event type")
            return await self._service.synchronize_edit(event.message)
        event = jobs[0].event
        if not isinstance(event, TelegramMessagesDeleted):
            raise TypeError("delete job contains the wrong event type")
        return await self._service.synchronize_delete(event)


class ForwardJobRunner:
    """Claim one ordered job batch at a time and persist every state transition."""

    def __init__(
        self,
        repository: ForwardJobRepository,
        processor: ForwardJobProcessor,
        *,
        max_attempts: int = 5,
        retry_base: float = 1.0,
        retry_max: float = 60.0,
        poll_interval: float = 0.25,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base < 0 or retry_max < retry_base:
            raise ValueError("retry delays must be non-negative and ordered")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._repository = repository
        self._processor = processor
        self._max_attempts = max_attempts
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._poll_interval = poll_interval
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._wake = asyncio.Event()
        self._stopping = False

    async def prepare(self) -> int:
        return await self._repository.recover_incomplete()

    async def enqueue(self, jobs: Sequence[PendingForwardJob]) -> int:
        inserted = await self._repository.enqueue(jobs)
        self.wake()
        return inserted

    def wake(self) -> None:
        self._wake.set()

    def request_stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def run(self) -> None:
        while not self._stopping:
            processed = await self.process_once()
            if processed:
                continue
            self._wake.clear()
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def process_once(self) -> bool:
        jobs = tuple(await self._repository.claim_due(self._clock()))
        if not jobs:
            return False
        job_ids = tuple(job.id for job in jobs)
        try:
            report = await self._processor.execute(jobs)
            errors = _report_errors(report)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report = None
            errors = (error,)

        if not errors:
            await self._repository.mark_succeeded(job_ids)
            self._log_completion(jobs, report)
            return True

        summary = _error_summary(errors)
        attempts = max(job.attempts for job in jobs)
        if attempts >= self._max_attempts or all(_is_permanent(error) for error in errors):
            await self._repository.mark_failed(job_ids, summary)
            self._logger.error(
                "forwarder job failed permanently",
                extra={
                    "feature": "forwarder",
                    "job_ids": job_ids,
                    "operation": jobs[0].kind.value,
                    "attempt": attempts,
                    "error_type": type(errors[0]).__name__,
                },
            )
            return True

        delay = _retry_delay(errors, attempts, self._retry_base, self._retry_max)
        await self._repository.reschedule(
            job_ids,
            available_at=self._clock() + delay,
            error=summary,
        )
        self._logger.warning(
            "forwarder job scheduled for retry",
            extra={
                "feature": "forwarder",
                "job_ids": job_ids,
                "operation": jobs[0].kind.value,
                "attempt": attempts,
                "retry_after": delay,
                "error_type": type(errors[0]).__name__,
            },
        )
        return True

    def _log_completion(
        self,
        jobs: Sequence[ForwardJob],
        report: ProcessingReport | None,
    ) -> None:
        extra: dict[str, object] = {
            "feature": "forwarder",
            "job_ids": tuple(job.id for job in jobs),
            "operation": jobs[0].kind.value,
            "attempt": max(job.attempts for job in jobs),
        }
        if isinstance(report, ForwardingReport):
            extra.update(
                matched_routes=report.matched_routes,
                delivered_messages=report.delivered_messages,
                deduplicated_messages=report.deduplicated_messages,
            )
        elif isinstance(report, SyncReport):
            extra["synchronized"] = report.synchronized
        self._logger.info("forwarder job completed", extra=extra)


def _received_message(job: ForwardJob) -> IncomingMessage:
    event = job.event
    if not isinstance(event, TelegramMessageReceived):
        raise TypeError("receive job contains the wrong event type")
    return event.message


def _report_errors(report: ProcessingReport) -> tuple[Exception, ...]:
    return tuple(failure.error for failure in report.failures)


def _is_permanent(error: Exception) -> bool:
    return isinstance(
        error,
        (
            DeliveryResultMismatch,
            MessageNotFound,
            NativeForwardUnsupported,
            PartialDeliveryState,
            PermanentDeliveryError,
        ),
    )


def _retry_delay(
    errors: Sequence[Exception],
    attempts: int,
    retry_base: float,
    retry_max: float,
) -> float:
    requested: list[float] = [error.seconds for error in errors if isinstance(error, RetryAfter)]
    if requested:
        return max(requested)
    return min(retry_base * (2.0 ** max(attempts - 1, 0)), retry_max)


def _error_summary(errors: Sequence[Exception]) -> str:
    return "; ".join(f"{type(error).__name__}: {error}" for error in errors)[:2000]
