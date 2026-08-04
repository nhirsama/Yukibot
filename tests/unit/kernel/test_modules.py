from __future__ import annotations

from dataclasses import dataclass

from yukibot.kernel import ModuleController


class MemoryModuleStates:
    def __init__(self) -> None:
        self.values: dict[str, bool] = {}

    async def get_enabled(self, name: str) -> bool | None:
        return self.values.get(name)

    async def set_enabled(self, name: str, *, enabled: bool) -> None:
        self.values[name] = enabled


@dataclass
class FakeModule:
    name: str
    starts: int = 0
    stops: int = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


async def test_module_state_is_persistent_and_runtime_changes_are_idempotent() -> None:
    states = MemoryModuleStates()
    module = FakeModule("forwarder")
    controller = ModuleController((module,), states)

    await controller.start()
    assert module.starts == 1
    assert states.values == {"forwarder": True}

    assert not (await controller.disable("forwarder")).running
    assert not (await controller.disable("forwarder")).running
    assert module.stops == 1
    assert states.values["forwarder"] is False

    assert (await controller.enable("forwarder")).running
    assert (await controller.enable("forwarder")).running
    assert module.starts == 2
    assert states.values["forwarder"] is True

    await controller.stop()
    assert module.stops == 2


async def test_disabled_module_stays_stopped_after_controller_restart() -> None:
    states = MemoryModuleStates()
    states.values["forwarder"] = False
    first = FakeModule("forwarder")

    controller = ModuleController((first,), states)
    await controller.start()
    assert first.starts == 0
    assert (await controller.list_modules())[0].enabled is False
    await controller.stop()

    second = FakeModule("forwarder")
    restarted = ModuleController((second,), states)
    await restarted.start()
    assert second.starts == 0
    await restarted.enable("forwarder")
    assert second.starts == 1
    await restarted.stop()
