from __future__ import annotations

from dataclasses import dataclass

import pytest

from yukibot.kernel import (
    DuplicateFeatureError,
    LifecycleManager,
    LifecycleStartError,
    LifecycleState,
    LifecycleStopError,
)


@dataclass
class StubFeature:
    name: str
    events: list[str]
    fail_start: bool = False
    fail_stop: bool = False

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError("start failed")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("stop failed")


async def test_start_in_order_and_stop_in_reverse_order() -> None:
    events: list[str] = []
    manager = LifecycleManager((StubFeature("one", events), StubFeature("two", events)))

    await manager.start()
    await manager.start()
    assert manager.state is LifecycleState.RUNNING
    assert manager.started_features == ("one", "two")

    await manager.stop()
    await manager.stop()
    assert manager.state is LifecycleState.STOPPED
    assert events == ["start:one", "start:two", "stop:two", "stop:one"]


async def test_start_failure_rolls_back_started_features() -> None:
    events: list[str] = []
    manager = LifecycleManager(
        (StubFeature("one", events), StubFeature("two", events, fail_start=True))
    )

    with pytest.raises(LifecycleStartError) as caught:
        await manager.start()

    assert caught.value.feature_name == "two"
    assert isinstance(caught.value.cause, RuntimeError)
    assert manager.started_features == ()
    assert manager.state is LifecycleState.FAILED
    assert events == ["start:one", "start:two", "stop:one"]


async def test_all_features_are_stopped_even_when_one_fails() -> None:
    events: list[str] = []
    manager = LifecycleManager(
        (StubFeature("one", events), StubFeature("two", events, fail_stop=True))
    )
    await manager.start()

    with pytest.raises(LifecycleStopError) as caught:
        await manager.stop()

    assert caught.value.failures[0][0] == "two"
    assert events[-2:] == ["stop:two", "stop:one"]
    assert manager.state is LifecycleState.FAILED


def test_duplicate_feature_names_are_rejected() -> None:
    events: list[str] = []
    with pytest.raises(DuplicateFeatureError, match="duplicate feature name"):
        LifecycleManager((StubFeature("same", events), StubFeature("same", events)))
