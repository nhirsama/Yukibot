"""The /summary command owned by the summarizer module."""

from __future__ import annotations

import re
import shlex

from yukibot.kernel import CommandResult, ControlCommand

from .errors import SummarizerError
from .models import SummaryEndpoint, SummaryModelConfig, SummaryPromptPreset, SummaryRule
from .prompts import PROMPT_PRESETS
from .service import SummarizerService

SUMMARY_HELP = """消息总结命令:
/summary list - 列出总结规则
/summary show <id> - 查看规则详情
/summary add <source> <destination> [时间窗] - 添加规则
/summary set <id> <source> <destination> [时间窗] - 更新规则
/summary run <id> [时间窗] - 立即生成并发送总结
/summary enable <id> - 启用规则
/summary disable <id> - 停用规则
/summary remove <id> - 删除规则
模型配置:
/summary model show
/summary model set <provider> <model> [-api-key <key>] [-base-url <url>]
/summary model tune <input_tokens> <output_tokens> <temperature> <timeout> <retries> [concurrency]
/summary model clear
总结提示词:
/summary prompt list
/summary prompt show
/summary prompt use <focused|decisions|technical|digest>
/summary prompt custom <自定义偏好>
/summary prompt clear
时间窗支持 30m、6h、1d, 默认 1d, 最大 30d。
source/destination 支持数字 ID、@用户名和 Telegram 公开链接。
群组话题支持 -100群组ID/话题ID、https://t.me/c/内部ID/话题ID,
以及 https://t.me/公开群用户名/话题ID。目标可以是私聊、频道、群组或群组话题。"""

_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhd])$")


class SummarizerCommands:
    def __init__(self, service: SummarizerService) -> None:
        self._service = service

    async def handle(self, command: ControlCommand) -> CommandResult:
        try:
            arguments = shlex.split(command.raw_arguments)
        except ValueError as error:
            return CommandResult(f"Invalid arguments: {error}")
        if not arguments or arguments == ["help"]:
            return CommandResult(SUMMARY_HELP)
        try:
            if arguments == ["model", "show"]:
                config = await self._service.get_model_config()
                return CommandResult(
                    _model_config(config)
                    if config is not None
                    else "Summary model is not configured."
                )
            if arguments == ["model", "clear"]:
                await self._service.clear_model_config()
                return CommandResult("Summary model configuration is cleared.")
            if arguments[:2] == ["model", "set"] and len(arguments) >= 4:
                options = _named_options(
                    arguments[4:],
                    allowed={"-api-key", "-base-url"},
                )
                config = await self._service.configure_model(
                    arguments[2],
                    arguments[3],
                    api_key=options.get("-api-key"),
                    base_url=options.get("-base-url"),
                )
                return CommandResult(f"Summary model is configured: {_qualified_model(config)}.")
            if arguments[:2] == ["model", "tune"] and len(arguments) in {7, 8}:
                config = await self._service.tune_model(
                    input_token_limit=int(arguments[2]),
                    output_token_limit=int(arguments[3]),
                    temperature=float(arguments[4]),
                    timeout=float(arguments[5]),
                    max_retries=int(arguments[6]),
                    max_concurrency=int(arguments[7]) if len(arguments) == 8 else None,
                )
                return CommandResult(_model_config(config))
            if arguments == ["prompt", "list"]:
                return CommandResult(
                    "\n".join(
                        f"{preset.value}: {definition.description}"
                        for preset, definition in PROMPT_PRESETS.items()
                    )
                )
            if arguments == ["prompt", "show"]:
                config = await self._service.get_model_config()
                if config is None:
                    raise SummarizerError("消息总结模型未配置, 请先配置模型。")
                return CommandResult(_prompt_config(config))
            if arguments[:2] == ["prompt", "use"] and len(arguments) == 3:
                config = await self._service.set_prompt_preset(arguments[2])
                return CommandResult(_prompt_config(config))
            if arguments[:2] == ["prompt", "custom"] and len(arguments) >= 3:
                config = await self._service.set_custom_prompt(" ".join(arguments[2:]))
                return CommandResult(_prompt_config(config))
            if arguments == ["prompt", "clear"]:
                config = await self._service.set_prompt_preset(SummaryPromptPreset.FOCUSED.value)
                return CommandResult(_prompt_config(config))
            if arguments == ["list"]:
                rules = await self._service.list_rules()
                return CommandResult(
                    "\n".join(_rule_summary(rule) for rule in rules) or "No summary rules."
                )
            if len(arguments) == 2 and arguments[0] == "show":
                return CommandResult(_rule_details(await self._service.get_rule(int(arguments[1]))))
            if arguments[0] == "add" and 3 <= len(arguments) <= 4:
                window = _window(arguments[3]) if len(arguments) == 4 else None
                rule = await self._service.add_rule(
                    arguments[1], arguments[2], window_seconds=window
                )
                return CommandResult(f"Summary rule {rule.id} is configured.")
            if arguments[0] == "set" and 4 <= len(arguments) <= 5:
                window = _window(arguments[4]) if len(arguments) == 5 else None
                rule = await self._service.replace_rule(
                    int(arguments[1]),
                    arguments[2],
                    arguments[3],
                    window_seconds=window,
                )
                return CommandResult(f"Summary rule {rule.id} is updated.")
            if arguments[0] == "run" and 2 <= len(arguments) <= 3:
                window = _window(arguments[2]) if len(arguments) == 3 else None
                execution = await self._service.run_rule(int(arguments[1]), window_seconds=window)
                return CommandResult(
                    f"总结已发送: 规则 {execution.rule.id}, "
                    f"消息 {execution.message_count}, 主题 {execution.topic_count}, "
                    f"发送 {len(execution.sent_messages)} 条。"
                )
            if len(arguments) == 2 and arguments[0] in {"enable", "disable"}:
                enabled = arguments[0] == "enable"
                rule = await self._service.set_enabled(int(arguments[1]), enabled=enabled)
                return CommandResult(
                    f"Summary rule {rule.id} is {'enabled' if enabled else 'disabled'}."
                )
            if len(arguments) == 2 and arguments[0] == "remove":
                rule_id = int(arguments[1])
                await self._service.remove_rule(rule_id)
                return CommandResult(f"Summary rule {rule_id} is removed.")
        except (SummarizerError, ValueError) as error:
            return CommandResult(error.args[0] if error.args else str(error))
        return CommandResult(SUMMARY_HELP)


def _window(value: str) -> int:
    match = _DURATION.fullmatch(value.casefold())
    if match is None:
        raise ValueError("时间窗格式应为 30m、6h 或 1d")
    amount = int(match.group("amount"))
    multiplier = {"m": 60, "h": 3600, "d": 86400}[match.group("unit")]
    seconds = amount * multiplier
    if not 60 <= seconds <= 30 * 86400:
        raise ValueError("时间窗必须在 1 分钟到 30 天之间")
    return seconds


def _rule_summary(rule: SummaryRule) -> str:
    state = "enabled" if rule.enabled else "disabled"
    return (
        f"{rule.id}: {_endpoint(rule.source)} -> {_endpoint(rule.destination)} "
        f"({_duration(rule.window_seconds)}, {state})"
    )


def _rule_details(rule: SummaryRule) -> str:
    return "\n".join(
        (
            _rule_summary(rule),
            f"source chat id: {rule.source.chat_id}",
            f"source topic id: {rule.source.topic_id or 'none'}",
            f"destination chat id: {rule.destination.chat_id}",
            f"destination topic id: {rule.destination.topic_id or 'none'}",
        )
    )


def _endpoint(endpoint: SummaryEndpoint) -> str:
    chat = f"@{endpoint.username}" if endpoint.username is not None else str(endpoint.chat_id)
    return chat if endpoint.topic_id is None else f"{chat}/{endpoint.topic_id}"


def _duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _named_options(arguments: list[str], *, allowed: set[str]) -> dict[str, str]:
    if len(arguments) % 2 != 0:
        raise ValueError("模型选项必须使用 -参数 值 的格式")
    parsed: dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        raw_name, value = arguments[index : index + 2]
        name = _normalize_option_name(raw_name)
        if name not in allowed:
            raise ValueError(f"未知模型选项: {name}")
        if name in parsed:
            raise ValueError(f"模型选项不能重复: {name}")
        if not value or value.startswith("--"):
            raise ValueError(f"模型选项缺少值: {name}")
        parsed[name] = value
    return parsed


def _normalize_option_name(value: str) -> str:
    """Accept Telegram's typographic dash replacement for command options."""
    source = "".join(chr(codepoint) for codepoint in (0x2014, 0x2013, 0x2212))
    normalized = value.translate(str.maketrans(source, "---"))
    return f"-{normalized.lstrip('-')}" if normalized.startswith("-") else normalized


def _model_config(config: SummaryModelConfig) -> str:
    active_prompt = "custom" if config.custom_prompt is not None else config.prompt_preset.value
    return "\n".join(
        (
            f"provider: {config.provider}",
            f"model: {config.model}",
            f"API key: {'configured' if config.api_key is not None else 'not configured'}",
            f"base URL: {config.base_url or 'provider default'}",
            f"input tokens: {config.input_token_limit}",
            f"output tokens: {config.output_token_limit}",
            f"temperature: {config.temperature:g}",
            f"timeout: {config.timeout:g}s",
            f"retries: {config.max_retries}",
            f"concurrency: {config.max_concurrency}",
            f"prompt: {active_prompt}",
        )
    )


def _prompt_config(config: SummaryModelConfig) -> str:
    if config.custom_prompt is not None:
        return "\n".join(
            (
                "prompt: custom",
                f"custom prompt: {config.custom_prompt}",
            )
        )
    definition = PROMPT_PRESETS[config.prompt_preset]
    return "\n".join(
        (
            f"prompt: {config.prompt_preset.value}",
            f"description: {definition.description}",
        )
    )


def _qualified_model(config: SummaryModelConfig) -> str:
    prefix = f"{config.provider}/"
    return config.model if config.model.casefold().startswith(prefix) else prefix + config.model
