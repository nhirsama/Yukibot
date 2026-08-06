"""Parse Telegram chats and forum-topic references without using Telethon."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_NUMERIC_TOPIC = re.compile(r"^(?P<chat>-?[1-9][0-9]*)/(?P<topic>[1-9][0-9]*)$")
_TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}


@dataclass(frozen=True, slots=True)
class EndpointReference:
    chat: int | str
    topic_id: int | None = None


def parse_endpoint_reference(value: str) -> EndpointReference:
    reference = value.strip()
    if not reference:
        raise ValueError("Telegram reference must not be empty")

    match = _NUMERIC_TOPIC.fullmatch(reference)
    if match is not None:
        return EndpointReference(int(match.group("chat")), int(match.group("topic")))

    try:
        return EndpointReference(int(reference))
    except ValueError:
        pass

    if reference.startswith("@") and len(reference) > 1 and "/" not in reference:
        return EndpointReference(reference)

    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() not in _TELEGRAM_HOSTS:
        raise ValueError("聊天引用必须是 ID、@用户名、公开链接或话题链接")
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts or parts[0].startswith("+") or parts[0] == "joinchat":
        raise ValueError("摘要规则暂不支持私有邀请链接")
    if parts[0] == "c":
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise ValueError("私有群话题链接格式不正确")
        return EndpointReference(int(f"-100{parts[1]}"), int(parts[2]))
    if len(parts) == 1:
        return EndpointReference(f"@{parts[0]}")
    if len(parts) == 2 and parts[1].isdigit():
        return EndpointReference(f"@{parts[0]}", int(parts[1]))
    raise ValueError("Telegram 公开链接格式不正确")
