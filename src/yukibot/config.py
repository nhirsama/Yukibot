"""Validated, immutable application configuration."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YUKIBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr = Field(min_length=1)
    telegram_session_path: Path = Path("data/yukibot.session")
    database_url: str = "sqlite:///data/yukibot.db"
    log_level: str = "INFO"
    forwarder_album_delay: float = Field(default=0.8, ge=0, le=10)
    shutdown_timeout: float = Field(default=15.0, gt=0, le=300)
    rebuild_join_min_interval: float = Field(default=300.0, ge=300, le=86400)
    rebuild_join_max_interval: float = Field(default=600.0, ge=300, le=86400)

    @field_validator("telegram_api_hash")
    @classmethod
    def validate_api_hash(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("telegram_api_hash must not be blank")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("only sqlite:/// database URLs are currently supported")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level: {value}")
        return normalized

    @model_validator(mode="after")
    def validate_rebuild_intervals(self) -> Settings:
        if self.rebuild_join_max_interval < self.rebuild_join_min_interval:
            raise ValueError("rebuild join intervals must be ordered")
        return self
