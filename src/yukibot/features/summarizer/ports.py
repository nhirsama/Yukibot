"""Ports owned by the summarizer feature."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from yukibot.contracts import MessageRef

from .models import (
    FetchedSummaryMessages,
    SummaryDocument,
    SummaryEndpoint,
    SummaryModelConfig,
    SummaryRule,
    SummaryRuleDraft,
    SummaryRun,
)


class SummaryRuleRepository(Protocol):
    async def list_all(self) -> Sequence[SummaryRule]: ...

    async def add_auto(self, draft: SummaryRuleDraft) -> SummaryRule: ...

    async def replace(self, rule: SummaryRule) -> None: ...

    async def remove(self, rule_id: int) -> bool: ...


class SummaryRunRepository(Protocol):
    async def save(self, run: SummaryRun) -> None: ...


class SummaryModelConfigRepository(Protocol):
    async def get_model_config(self) -> SummaryModelConfig | None: ...

    async def save_model_config(self, config: SummaryModelConfig) -> None: ...

    async def clear_model_config(self) -> bool: ...


class SummaryTelegram(Protocol):
    async def resolve_endpoint(self, reference: str) -> SummaryEndpoint: ...

    async def fetch_recent(
        self,
        source: SummaryEndpoint,
        *,
        since: datetime,
        limit: int | None = None,
    ) -> FetchedSummaryMessages: ...

    async def send_text(self, destination: SummaryEndpoint, text: str) -> MessageRef: ...


class StructuredSummaryGenerator(Protocol):
    async def reset(self) -> None: ...

    async def generate(
        self,
        *,
        config: SummaryModelConfig,
        system_prompt: str,
        user_prompt: str,
    ) -> SummaryDocument: ...
