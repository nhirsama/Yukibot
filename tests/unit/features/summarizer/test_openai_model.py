from types import SimpleNamespace

import pytest

from yukibot.features.summarizer.errors import SummaryModelUnavailableError
from yukibot.features.summarizer.infrastructure import OpenAISummaryGenerator, openai_model
from yukibot.features.summarizer.models import SummaryModelConfig


@pytest.mark.parametrize("base_url", [None, "https://models.example/v1"])
async def test_generator_uses_only_official_responses_request(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str | None,
) -> None:
    client_calls: list[dict[str, object]] = []
    stream_calls: list[dict[str, object]] = []
    close_calls = 0
    output_text = (
        '{"topics":[{"title":"Release","summary":"Version one shipped.",'
        '"evidence_message_ids":[10],"action_items":[{"task":"Verify",'
        '"owner":"Alice"}]}]}'
    )

    class BrokenCompletedResponse:
        @property
        def output_text(self) -> str:
            return "".join([None])  # type: ignore[list-item]

    class FakeStream:
        def __init__(self) -> None:
            self._events = iter(
                (
                    SimpleNamespace(type="response.in_progress"),
                    SimpleNamespace(type="response.output_text.delta", delta=output_text),
                    SimpleNamespace(
                        type="response.completed",
                        response=BrokenCompletedResponse(),
                    ),
                    SimpleNamespace(type="ping"),
                )
            )

        async def __aenter__(self) -> "FakeStream":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def __aiter__(self) -> "FakeStream":
            return self

        async def __anext__(self) -> object:
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration from None

    def stream(**kwargs: object) -> FakeStream:
        stream_calls.append(kwargs)
        return FakeStream()

    class FakeOpenAIClient:
        responses = SimpleNamespace(stream=stream)

        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    def async_openai(**kwargs: object) -> object:
        client_calls.append(kwargs)
        return FakeOpenAIClient()

    monkeypatch.setattr(
        "yukibot.features.summarizer.infrastructure.openai_model.AsyncOpenAI",
        async_openai,
    )
    generator = OpenAISummaryGenerator()
    config = SummaryModelConfig(
        "openai",
        "deepseek-v4-flash",
        api_key="secret",
        base_url=base_url,
    )

    document = await generator.generate(
        config=config,
        system_prompt="system",
        user_prompt="user",
    )
    await generator.reset()

    expected_client_call: dict[str, object] = {
        "api_key": "secret",
        "max_retries": 2,
        "default_headers": {"User-Agent": "yukibot/0.1.0"},
    }
    if base_url is not None:
        expected_client_call["base_url"] = base_url
    assert client_calls == [expected_client_call]
    assert close_calls == 1
    assert document.topics[0].evidence_message_ids == (10,)
    assert document.topics[0].action_items[0].owner == "Alice"

    request = stream_calls[0]
    assert request["model"] == "deepseek-v4-flash"
    assert request["timeout"] == 120
    assert "JSON Schema:" in str(request["input"])
    for unsupported in ("max_output_tokens", "temperature", "tools", "tool_choice"):
        assert unsupported not in request


async def test_generator_rejects_non_openai_provider() -> None:
    generator = OpenAISummaryGenerator()

    with pytest.raises(SummaryModelUnavailableError, match="provider must be openai"):
        await generator.generate(
            config=SummaryModelConfig("apiarc", "deepseek-v4-flash"),
            system_prompt="system",
            user_prompt="user",
        )


async def test_generator_retries_in_stream_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []
    output_text = (
        '{"topics":[{"title":"Release","summary":"Version one shipped.",'
        '"evidence_message_ids":[10]}]}'
    )

    async def stream_text(*args: object, **kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise openai_model._UpstreamResponseError("upstream failed")
        return output_text

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(openai_model, "_stream_text", stream_text)
    monkeypatch.setattr(openai_model.asyncio, "sleep", sleep)
    generator = OpenAISummaryGenerator()
    try:
        document = await generator.generate(
            config=SummaryModelConfig("openai", "deepseek-v4-flash", api_key="secret"),
            system_prompt="system",
            user_prompt="user",
        )
    finally:
        await generator.reset()

    assert attempts == 2
    assert delays == [1]
    assert document.topics[0].evidence_message_ids == (10,)
