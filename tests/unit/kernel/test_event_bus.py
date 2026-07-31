import asyncio

import pytest

from yukibot.kernel import InProcessEventBus


async def test_handlers_run_concurrently_and_failure_is_isolated() -> None:
    bus = InProcessEventBus()
    both_started = asyncio.Event()
    started = 0
    received: list[int] = []

    async def successful(event: int) -> None:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)
        received.append(event)

    async def failing(event: int) -> None:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)
        raise RuntimeError(f"bad event {event}")

    bus.subscribe(int, successful)
    bus.subscribe(int, failing)

    report = await bus.publish(7)

    assert received == [7]
    assert report.handler_count == 2
    assert report.succeeded == 1
    assert len(report.failures) == 1
    assert isinstance(report.failures[0].error, RuntimeError)


async def test_subscription_is_idempotently_removable() -> None:
    bus = InProcessEventBus()
    events: list[str] = []

    async def handler(event: str) -> None:
        events.append(event)

    subscription = bus.subscribe(str, handler)
    assert subscription.active
    await bus.publish("first")
    subscription.unsubscribe()
    subscription.unsubscribe()
    report = await bus.publish("second")

    assert events == ["first"]
    assert not subscription.active
    assert report.handler_count == 0


def test_duplicate_subscription_is_rejected() -> None:
    bus = InProcessEventBus()

    async def handler(event: str) -> None:
        return None

    bus.subscribe(str, handler)
    with pytest.raises(ValueError, match="already subscribed"):
        bus.subscribe(str, handler)


async def test_dispatch_uses_exact_event_type() -> None:
    class Parent:
        pass

    class Child(Parent):
        pass

    bus = InProcessEventBus()
    called = False

    async def handler(event: Parent) -> None:
        nonlocal called
        called = True

    bus.subscribe(Parent, handler)
    report = await bus.publish(Child())

    assert not called
    assert report.handler_count == 0
