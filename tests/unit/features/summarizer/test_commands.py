from __future__ import annotations

from dataclasses import replace

from yukibot.contracts import MessageRef
from yukibot.features.summarizer.commands import SUMMARY_HELP, SummarizerCommands
from yukibot.features.summarizer.errors import SummarizerError
from yukibot.features.summarizer.models import (
    SummaryEndpoint,
    SummaryExecution,
    SummaryModelConfig,
    SummaryPromptPreset,
    SummaryRule,
)
from yukibot.kernel import ControlCommand


class FakeService:
    def __init__(self) -> None:
        self.rule = SummaryRule(
            1,
            SummaryEndpoint(-1001, username="source"),
            SummaryEndpoint(-1002, topic_id=42, username="target"),
            21600,
        )
        self.calls: list[tuple[object, ...]] = []
        self.error: Exception | None = None
        self.model_config: SummaryModelConfig | None = None

    async def get_model_config(self) -> SummaryModelConfig | None:
        return self.model_config

    async def configure_model(
        self,
        provider: str,
        model: str,
        *,
        api_key: str | None,
        base_url: str | None,
    ) -> SummaryModelConfig:
        self.calls.append(("model-set", provider, model, api_key, base_url))
        self.model_config = SummaryModelConfig(provider, model, api_key, base_url)
        return self.model_config

    async def tune_model(
        self,
        *,
        input_token_limit: int,
        output_token_limit: int,
        temperature: float,
        timeout: float,
        max_retries: int,
        max_concurrency: int | None = None,
    ) -> SummaryModelConfig:
        self.calls.append(
            (
                "model-tune",
                input_token_limit,
                output_token_limit,
                temperature,
                timeout,
                max_retries,
                max_concurrency,
            )
        )
        assert self.model_config is not None
        self.model_config = replace(
            self.model_config,
            input_token_limit=input_token_limit,
            output_token_limit=output_token_limit,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            max_concurrency=(
                self.model_config.max_concurrency if max_concurrency is None else max_concurrency
            ),
        )
        return self.model_config

    async def set_prompt_preset(self, preset: str) -> SummaryModelConfig:
        self.calls.append(("prompt-use", preset))
        assert self.model_config is not None
        self.model_config = replace(
            self.model_config,
            prompt_preset=SummaryPromptPreset(preset),
            custom_prompt=None,
        )
        return self.model_config

    async def set_custom_prompt(self, prompt: str) -> SummaryModelConfig:
        self.calls.append(("prompt-custom", prompt))
        assert self.model_config is not None
        self.model_config = replace(self.model_config, custom_prompt=prompt)
        return self.model_config

    async def clear_model_config(self) -> bool:
        self.calls.append(("model-clear",))
        existed = self.model_config is not None
        self.model_config = None
        return existed

    async def list_rules(self) -> tuple[SummaryRule, ...]:
        return (self.rule,)

    async def get_rule(self, rule_id: int) -> SummaryRule:
        return self.rule

    async def add_rule(
        self,
        source: str,
        destination: str,
        *,
        window_seconds: int | None = None,
    ) -> SummaryRule:
        self.calls.append(("add", source, destination, window_seconds))
        return self.rule

    async def replace_rule(self, *args: object, **kwargs: object) -> SummaryRule:
        self.calls.append(("set", *args, kwargs))
        return self.rule

    async def run_rule(
        self, rule_id: int, *, window_seconds: int | None = None
    ) -> SummaryExecution:
        self.calls.append(("run", rule_id, window_seconds))
        if self.error is not None:
            raise self.error
        return SummaryExecution(self.rule, 8, 2, (MessageRef(-1002, 50),))

    async def set_enabled(self, rule_id: int, *, enabled: bool) -> SummaryRule:
        self.calls.append(("enabled", rule_id, enabled))
        return self.rule

    async def remove_rule(self, rule_id: int) -> None:
        self.calls.append(("remove", rule_id))


def command(arguments: str) -> ControlCommand:
    return ControlCommand("/summary", arguments, -100, 1, 999, True)


async def test_commands_accept_topic_destination_and_duration() -> None:
    service = FakeService()
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    added = await commands.handle(command("add -1001 https://t.me/c/2001/42 6h"))
    run = await commands.handle(command("run 1 30m"))

    assert added.text == "Summary rule 1 is configured."
    assert run.text == "总结已发送: 规则 1, 消息 8, 主题 2, 发送 1 条。"
    assert service.calls == [
        ("add", "-1001", "https://t.me/c/2001/42", 21600),
        ("run", 1, 1800),
    ]


async def test_commands_render_rules_and_return_domain_errors() -> None:
    service = FakeService()
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    listing = await commands.handle(command("list"))
    details = await commands.handle(command("show 1"))
    service.error = SummarizerError("模型未配置")
    failed = await commands.handle(command("run 1"))

    assert listing.text == "1: @source -> @target/42 (6h, enabled)"
    assert details.text is not None
    assert "destination topic id: 42" in details.text
    assert failed.text == "模型未配置"
    assert (await commands.handle(command("add only-one"))).text == SUMMARY_HELP


async def test_model_configuration_commands_are_persisted_and_redacted() -> None:
    service = FakeService()
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    configured = await commands.handle(
        command("model set openai gpt-test -api-key top-secret -base-url https://models.example/v1")
    )
    shown = await commands.handle(command("model show"))
    tuned = await commands.handle(command("model tune 16000 2000 0.2 60 3"))
    cleared = await commands.handle(command("model clear"))

    assert configured.text == "Summary model is configured: openai/gpt-test."
    assert shown.text is not None
    assert "API key: configured" in shown.text
    assert "top-secret" not in shown.text
    assert tuned.text is not None
    assert "input tokens: 16000" in tuned.text
    assert cleared.text == "Summary model configuration is cleared."
    assert service.calls == [
        (
            "model-set",
            "openai",
            "gpt-test",
            "top-secret",
            "https://models.example/v1",
        ),
        ("model-tune", 16000, 2000, 0.2, 60.0, 3, None),
        ("model-clear",),
    ]


async def test_prompt_commands_list_select_customize_and_clear() -> None:
    service = FakeService()
    service.model_config = SummaryModelConfig("openai", "gpt-test")
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    presets = await commands.handle(command("prompt list"))
    selected = await commands.handle(command("prompt use technical"))
    customized = await commands.handle(command("prompt custom 只保留发布版本、故障根因和验证结果"))
    shown = await commands.handle(command("prompt show"))
    cleared = await commands.handle(command("prompt clear"))

    assert presets.text is not None
    assert all(name in presets.text for name in ("focused", "decisions", "technical", "digest"))
    assert selected.text is not None and "prompt: technical" in selected.text
    assert customized.text is not None and "prompt: custom" in customized.text
    assert shown.text is not None and "故障根因" in shown.text
    assert cleared.text is not None and "prompt: focused" in cleared.text
    assert service.calls == [
        ("prompt-use", "technical"),
        ("prompt-custom", "只保留发布版本、故障根因和验证结果"),
        ("prompt-use", "focused"),
    ]


async def test_model_tune_accepts_and_displays_concurrency() -> None:
    service = FakeService()
    service.model_config = SummaryModelConfig("openai", "gpt-test")
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    tuned = await commands.handle(command("model tune 16000 2000 0.2 60 3 5"))

    assert tuned.text is not None
    assert "concurrency: 5" in tuned.text
    assert service.calls == [("model-tune", 16000, 2000, 0.2, 60.0, 3, 5)]


async def test_model_configuration_accepts_telegram_typographic_dashes() -> None:
    service = FakeService()
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    configured = await commands.handle(
        command(
            "model set openai gpt-test "
            "\u2014api-key top-secret \u2013base-url https://models.example/v1"
        )
    )

    assert configured.text == "Summary model is configured: openai/gpt-test."
    assert service.model_config is not None
    assert service.model_config.api_key == "top-secret"
    assert service.model_config.base_url == "https://models.example/v1"


async def test_model_configuration_does_not_duplicate_provider_prefix() -> None:
    service = FakeService()
    commands = SummarizerCommands(service)  # type: ignore[arg-type]

    configured = await commands.handle(
        command(
            "model set apiarc apiarc/deepseek-v4-flash-free "
            "-api-key top-secret -base-url https://apiarc.ai/v1"
        )
    )

    assert configured.text == ("Summary model is configured: apiarc/deepseek-v4-flash-free.")
