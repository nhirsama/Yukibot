"""Structured summaries through the official OpenAI Responses SDK."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import AsyncOpenAI
from openai.types.responses import EasyInputMessageParam, ResponseInputParam
from pydantic import BaseModel, ConfigDict, Field

from ..errors import SummaryModelUnavailableError
from ..models import SummaryActionItem, SummaryDocument, SummaryModelConfig, SummaryTopic

_USER_AGENT = "yukibot/0.1.0"


class _ActionItemOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task: str = Field(min_length=1, max_length=500)
    owner: str | None = Field(default=None, max_length=200)
    deadline: str | None = Field(default=None, max_length=200)


class _TopicOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    evidence_message_ids: list[int] = Field(min_length=1, max_length=10)
    participants: list[str] = Field(default_factory=list, max_length=30)
    decisions: list[str] = Field(default_factory=list, max_length=10)
    action_items: list[_ActionItemOutput] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class _SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topics: list[_TopicOutput] = Field(min_length=1, max_length=12)


class _RetryableResponseError(RuntimeError):
    pass


class OpenAISummaryGenerator:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._config: SummaryModelConfig | None = None

    async def reset(self) -> None:
        client, self._client = self._client, None
        self._config = None
        if client is not None:
            await client.close()

    async def generate(
        self,
        *,
        config: SummaryModelConfig,
        system_prompt: str,
        user_prompt: str,
    ) -> SummaryDocument:
        try:
            if config.provider != "openai":
                raise ValueError("summary model provider must be openai")
            if self._config != config:
                await self.reset()
                self._client = _openai_client(config)
                self._config = config
            if self._client is None:
                raise RuntimeError("OpenAI client was not initialized")
            input_items = _input(system_prompt, user_prompt)
            for attempt in range(config.max_retries + 1):
                try:
                    output_text = await _stream_text(
                        self._client,
                        model=config.model,
                        input_items=input_items,
                        timeout=config.timeout,
                    )
                    break
                except _RetryableResponseError:
                    if attempt == config.max_retries:
                        raise
                    await asyncio.sleep(2**attempt)
            output = _SummaryOutput.model_validate_json(output_text)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise SummaryModelUnavailableError(
                f"消息总结模型调用失败 ({type(error).__name__}): {error}"
            ) from error
        return _to_document(output)


def _openai_client(config: SummaryModelConfig) -> AsyncOpenAI:
    arguments: dict[str, Any] = {
        "api_key": config.api_key,
        "max_retries": config.max_retries,
        "default_headers": {"User-Agent": _USER_AGENT},
    }
    if config.base_url is not None:
        arguments["base_url"] = config.base_url
    return AsyncOpenAI(**arguments)


def _input(system_prompt: str, user_prompt: str) -> ResponseInputParam:
    schema = json.dumps(
        _SummaryOutput.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return [
        EasyInputMessageParam(
            role="system",
            content=(
                f"{system_prompt}\n"
                "只返回符合下面 JSON Schema 的 JSON 对象, 不要使用 Markdown 或附加说明。\n"
                f"JSON Schema: {schema}"
            ),
        ),
        EasyInputMessageParam(role="user", content=user_prompt),
    ]


async def _stream_text(
    client: AsyncOpenAI,
    *,
    model: str,
    input_items: ResponseInputParam,
    timeout: float,
) -> str:
    chunks: list[str] = []
    async with client.responses.stream(
        model=model,
        input=input_items,
        timeout=timeout,
    ) as stream:
        async for event in stream:
            if event.type == "response.output_text.delta" and isinstance(event.delta, str):
                chunks.append(event.delta)
            elif event.type == "response.failed":
                detail = event.response.error
                if detail is not None and str(detail.code) == "upstream_error":
                    raise _RetryableResponseError(str(detail))
                raise RuntimeError(f"Responses stream ended with {event.type}: {detail}")
            elif event.type == "response.incomplete":
                raise RuntimeError(
                    "Responses stream ended with response.incomplete: "
                    f"{event.response.incomplete_details}"
                )
            elif event.type == "error":
                raise RuntimeError(f"Responses stream error: {event.message}")
    output_text = "".join(chunks)
    if not output_text.strip():
        raise _RetryableResponseError("Responses API did not return output text")
    return output_text


def _to_document(output: _SummaryOutput) -> SummaryDocument:
    return SummaryDocument(
        tuple(
            SummaryTopic(
                topic.title,
                topic.summary,
                tuple(topic.evidence_message_ids),
                tuple(topic.participants),
                tuple(topic.decisions),
                tuple(
                    SummaryActionItem(item.task, item.owner, item.deadline)
                    for item in topic.action_items
                ),
                tuple(topic.open_questions),
            )
            for topic in output.topics
        )
    )
