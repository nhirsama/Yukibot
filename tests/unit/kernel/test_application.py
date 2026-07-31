import asyncio

from yukibot.kernel import Application, LifecycleManager, LifecycleState, TaskSupervisor


class BlockingFeature:
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = False

    async def start(self) -> None:
        self.started.set()

    async def stop(self) -> None:
        self.stopped = True


async def test_application_runs_until_explicit_shutdown() -> None:
    feature = BlockingFeature()
    lifecycle = LifecycleManager((feature,))
    app = Application(lifecycle, TaskSupervisor())

    run = asyncio.create_task(app.run(install_signal_handlers=False))
    await feature.started.wait()
    app.request_shutdown("test")
    await asyncio.wait_for(run, timeout=0.2)

    assert feature.stopped
    assert app.shutdown.reason == "test"
    assert lifecycle.state is LifecycleState.STOPPED


async def test_critical_task_failure_requests_shutdown() -> None:
    feature = BlockingFeature()
    lifecycle = LifecycleManager((feature,))
    supervisor = TaskSupervisor()
    app = Application(lifecycle, supervisor)
    run = asyncio.create_task(app.run(install_signal_handlers=False))
    await feature.started.wait()

    async def fail() -> None:
        raise RuntimeError("critical")

    failure = supervisor.create_task(fail(), name="critical", critical=True)
    await asyncio.gather(failure, return_exceptions=True)
    await asyncio.wait_for(run, timeout=0.2)

    assert app.shutdown.reason == "critical_task_failed"
    assert feature.stopped
