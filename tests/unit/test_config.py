from pathlib import Path

import pytest
from pydantic import ValidationError

from yukibot.config import Settings


def test_settings_parse_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YUKIBOT_TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("YUKIBOT_TELEGRAM_API_HASH", "secret-hash")
    monkeypatch.setenv("YUKIBOT_ADMIN_IDS", "10, 20,-30")
    monkeypatch.setenv("YUKIBOT_LOG_LEVEL", "debug")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.telegram_api_id == 12345
    assert settings.telegram_api_hash.get_secret_value() == "secret-hash"
    assert settings.admin_ids == (10, 20, -30)
    assert settings.log_level == "DEBUG"
    assert settings.telegram_session_path == Path("data/yukibot.session")
    assert "secret-hash" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("telegram_api_id", 0, "greater than 0"),
        ("database_url", "postgresql://localhost/test", "only sqlite"),
        ("log_level", "verbose", "unknown log level"),
        ("forwarder_workers", 0, "greater than or equal to 1"),
    ],
)
def test_invalid_settings_are_rejected(field: str, value: object, error: str) -> None:
    data: dict[str, object] = {
        "telegram_api_id": 1,
        "telegram_api_hash": "hash",
        field: value,
    }
    with pytest.raises(ValidationError, match=error):
        Settings(**data)  # type: ignore[arg-type]


def test_settings_are_immutable() -> None:
    settings = Settings(telegram_api_id=1, telegram_api_hash="hash")
    with pytest.raises(ValidationError, match="frozen"):
        settings.log_level = "DEBUG"  # type: ignore[misc]
