import asyncio

import pytest

from yukibot.kernel import SupervisorClosedError, TaskSupervisor


async def test_supervisor_records_failures_and_signals_critical_failure() -> None:
    supervisor = TaskSupervisor()

    async def fail() -> None:
        raise RuntimeError("worker failed")

    task = supervisor.create_task(fail(), name="worker", critical=True)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert supervisor.active_count == 0
    assert supervisor.failure_event.is_set()
    assert len(supervisor.failures) == 1
    assert supervisor.failures[0].task_name == "worker"
    assert supervisor.failures[0].critical


async def test_stop_cancels_owned_tasks_and_rejects_new_tasks() -> None:
    supervisor = TaskSupervisor()
    cancelled = asyncio.Event()

    async def worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor.create_task(worker(), name="worker")
    await asyncio.sleep(0)
    await supervisor.stop(timeout=0.1)

    assert cancelled.is_set()
    assert supervisor.active_count == 0

    coroutine = worker()
    with pytest.raises(SupervisorClosedError):
        supervisor.create_task(coroutine, name="late")
    assert coroutine.cr_frame is None


async def test_stop_timeout_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        await TaskSupervisor().stop(timeout=-1)
