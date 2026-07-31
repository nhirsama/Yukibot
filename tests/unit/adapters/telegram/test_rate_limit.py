import asyncio

from yukibot.adapters.telegram import TelegramRequestLimiter


async def test_same_chat_operations_are_serialized() -> None:
    limiter = TelegramRequestLimiter(max_concurrency=2, messages_per_second=100)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with limiter.slot(-1001):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with limiter.slot(-1001):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_different_chats_can_use_global_capacity_concurrently() -> None:
    limiter = TelegramRequestLimiter(max_concurrency=2, messages_per_second=100)
    both_entered = asyncio.Event()
    count = 0

    async def operation(chat_id: int) -> None:
        nonlocal count
        async with limiter.slot(chat_id):
            count += 1
            if count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=0.1)

    await asyncio.gather(operation(-1001), operation(-1002))
