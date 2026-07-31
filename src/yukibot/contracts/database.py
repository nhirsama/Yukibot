"""Small database contract used by feature-owned repositories."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

type SqlValue = str | int | float | bytes | None
type SqlParameters = Sequence[SqlValue] | Mapping[str, SqlValue]
type Row = Mapping[str, SqlValue]


class DatabaseError(Exception):
    """Base error exposed by a database adapter."""


class IntegrityViolation(DatabaseError):
    """A database uniqueness or referential constraint was violated."""


@dataclass(frozen=True, slots=True)
class Migration:
    scope: str
    version: int
    description: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.scope or not self.scope.replace("_", "").isalnum():
            raise ValueError("migration scope must contain letters, numbers or underscores")
        if self.version <= 0:
            raise ValueError("migration version must be positive")
        if not self.statements or any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements must not be empty")

    @property
    def checksum(self) -> str:
        payload = "\0".join(self.statements).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecuteResult:
    row_count: int
    last_row_id: int | None = None


class DatabaseConnection(Protocol):
    async def execute(self, sql: str, parameters: SqlParameters = ()) -> ExecuteResult: ...

    async def executemany(self, sql: str, parameters: Sequence[SqlParameters]) -> ExecuteResult: ...

    async def fetch_one(self, sql: str, parameters: SqlParameters = ()) -> Row | None: ...

    async def fetch_all(self, sql: str, parameters: SqlParameters = ()) -> Sequence[Row]: ...


class Database(DatabaseConnection, Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> bool: ...

    def transaction(self) -> AbstractAsyncContextManager[DatabaseConnection]: ...
