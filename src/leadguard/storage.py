from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4

from leadguard.domain import (
    Conversation,
    Enforcement,
    Lifecycle,
    ModelDecision,
    TurnResult,
    enforce_state_machine,
)


class ConversationNotFoundError(LookupError):
    pass


class RequestIdContentMismatchError(RuntimeError):
    """The same request_id was replayed with different message content."""


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SendAttempt:
    sent: bool
    lifecycle: Lifecycle
    next_allowed_at: datetime | None = None


class MessageRecord(TypedDict):
    id: int
    sender: str
    content: str
    request_id: str | None
    created_at: datetime


class SQLiteStore:
    """Small durable store; every mutation uses a real SQLite transaction."""

    def __init__(self, path: Path, rate_limit_seconds: int = 60) -> None:
        self.path = path
        self.rate_limit_seconds = rate_limit_seconds

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    lifecycle TEXT NOT NULL CHECK (
                        lifecycle IN ('active', 'human_controlled', 'not_interested')
                    ),
                    strike_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        strike_count BETWEEN 0 AND 2
                    ),
                    followup_pending INTEGER NOT NULL DEFAULT 0 CHECK (
                        followup_pending IN (0, 1)
                    ),
                    activation_epoch INTEGER NOT NULL DEFAULT 0 CHECK (
                        activation_epoch >= 0
                    ),
                    last_outbound_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    sender TEXT NOT NULL CHECK (sender IN ('customer', 'agent', 'system')),
                    content TEXT NOT NULL,
                    request_id TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE (conversation_id, request_id)
                );

                CREATE TABLE IF NOT EXISTS turns (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (conversation_id, request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
                    ON messages (conversation_id, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_turns_conversation_time
                    ON turns (conversation_id, created_at);
                """
            )

    def clear(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute("DELETE FROM turns")
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM conversations")

    def create_conversation(
        self,
        name: str,
        now: datetime,
        *,
        conversation_id: str | None = None,
    ) -> Conversation:
        identifier = conversation_id or str(uuid4())
        timestamp = _timestamp(now)
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    id, name, lifecycle, strike_count, followup_pending, activation_epoch,
                    last_outbound_at, created_at, updated_at
                ) VALUES (?, ?, 'active', 0, 0, 0, NULL, ?, ?)
                """,
                (identifier, name, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (identifier,)
            ).fetchone()
        assert row is not None
        return _conversation_from_row(row)

    def list_conversations(self) -> list[Conversation]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        return _conversation_from_row(row)

    def get_inbound_content(self, conversation_id: str, request_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content FROM messages
                WHERE conversation_id = ? AND request_id = ? AND sender = 'customer'
                """,
                (conversation_id, request_id),
            ).fetchone()
        return None if row is None else str(row["content"])

    def get_turn_result(self, conversation_id: str, request_id: str) -> TurnResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_json FROM turns
                WHERE conversation_id = ? AND request_id = ?
                """,
                (conversation_id, request_id),
            ).fetchone()
        if row is None:
            return None
        return TurnResult.model_validate_json(row["result_json"])

    def record_inbound(
        self,
        conversation_id: str,
        request_id: str,
        content: str,
        now: datetime,
    ) -> Conversation:
        """Persist the customer message exactly once per request_id.

        A replay with identical content is accepted silently so that a client
        (or this service, after crashing between the inbound write and the
        turn-result write) can retry the same request_id and have the turn
        processed to completion instead of being stuck behind a 409 forever.
        Reusing a request_id with *different* content is a client bug and is
        rejected.
        """

        with self._transaction(immediate=True) as connection:
            row = self._require_conversation(connection, conversation_id)
            try:
                connection.execute(
                    """
                    INSERT INTO messages (
                        conversation_id, sender, content, request_id, created_at
                    ) VALUES (?, 'customer', ?, ?, ?)
                    """,
                    (conversation_id, content, request_id, _timestamp(now)),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (_timestamp(now), conversation_id),
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    """
                    SELECT content FROM messages
                    WHERE conversation_id = ? AND request_id = ?
                    """,
                    (conversation_id, request_id),
                ).fetchone()
                if existing is None or existing["content"] != content:
                    raise RequestIdContentMismatchError(request_id) from error
        return _conversation_from_row(row)

    def apply_decision(
        self,
        conversation_id: str,
        decision: ModelDecision,
        expected_activation_epoch: int,
        now: datetime,
    ) -> Enforcement | None:
        """Re-check lifecycle and apply the deterministic policy in one write lock."""

        with self._transaction(immediate=True) as connection:
            row = self._require_conversation(connection, conversation_id)
            state = _conversation_from_row(row)
            if (
                state.lifecycle is not Lifecycle.ACTIVE
                or state.activation_epoch != expected_activation_epoch
            ):
                return None

            enforcement = enforce_state_machine(state, decision)
            connection.execute(
                """
                UPDATE conversations
                SET lifecycle = ?, strike_count = ?, followup_pending = ?,
                    activation_epoch = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    enforcement.lifecycle.value,
                    enforcement.strike_count,
                    int(enforcement.followup_pending),
                    enforcement.activation_epoch,
                    _timestamp(now),
                    conversation_id,
                ),
            )
        return enforcement

    def send_reply(
        self,
        conversation_id: str,
        content: str,
        expected_activation_epoch: int,
        now: datetime,
    ) -> SendAttempt:
        """The single final send boundary, with an atomic sliding-window check."""

        with self._transaction(immediate=True) as connection:
            row = self._require_conversation(connection, conversation_id)
            state = _conversation_from_row(row)
            if (
                state.lifecycle is not Lifecycle.ACTIVE
                or state.activation_epoch != expected_activation_epoch
            ):
                return SendAttempt(sent=False, lifecycle=state.lifecycle)

            if state.last_outbound_at is not None:
                next_allowed_at = state.last_outbound_at + timedelta(
                    seconds=self.rate_limit_seconds
                )
                if now < next_allowed_at:
                    return SendAttempt(
                        sent=False,
                        lifecycle=state.lifecycle,
                        next_allowed_at=next_allowed_at,
                    )

            timestamp = _timestamp(now)
            connection.execute(
                """
                INSERT INTO messages (conversation_id, sender, content, created_at)
                VALUES (?, 'agent', ?, ?)
                """,
                (conversation_id, content, timestamp),
            )
            connection.execute(
                """
                UPDATE conversations
                SET last_outbound_at = ?, followup_pending = 0, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, conversation_id),
            )
        return SendAttempt(sent=True, lifecycle=Lifecycle.ACTIVE)

    def force_escalate(
        self,
        conversation_id: str,
        expected_activation_epoch: int,
        now: datetime,
    ) -> Conversation | None:
        """Fail closed without giving the model any state-changing capability."""

        with self._transaction(immediate=True) as connection:
            row = self._require_conversation(connection, conversation_id)
            state = _conversation_from_row(row)
            if (
                state.lifecycle is not Lifecycle.ACTIVE
                or state.activation_epoch != expected_activation_epoch
            ):
                return None
            connection.execute(
                """
                UPDATE conversations
                SET lifecycle = 'human_controlled', followup_pending = 0,
                    activation_epoch = activation_epoch + 1, updated_at = ?
                WHERE id = ?
                """,
                (_timestamp(now), conversation_id),
            )
            updated = self._require_conversation(connection, conversation_id)
        return _conversation_from_row(updated)

    def append_system_message(self, conversation_id: str, content: str, now: datetime) -> None:
        with self._transaction(immediate=True) as connection:
            self._require_conversation(connection, conversation_id)
            connection.execute(
                """
                INSERT INTO messages (conversation_id, sender, content, created_at)
                VALUES (?, 'system', ?, ?)
                """,
                (conversation_id, content, _timestamp(now)),
            )

    def save_turn(self, result: TurnResult, now: datetime) -> TurnResult:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO turns (
                    conversation_id, request_id, result_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    result.conversation_id,
                    result.request_id,
                    result.model_dump_json(),
                    _timestamp(now),
                ),
            )
            row = connection.execute(
                """
                SELECT result_json FROM turns
                WHERE conversation_id = ? AND request_id = ?
                """,
                (result.conversation_id, result.request_id),
            ).fetchone()
        assert row is not None
        return TurnResult.model_validate_json(row["result_json"])

    def reactivate(self, conversation_id: str, now: datetime) -> Conversation:
        with self._transaction(immediate=True) as connection:
            row = self._require_conversation(connection, conversation_id)
            state = _conversation_from_row(row)
            if state.lifecycle is Lifecycle.NOT_INTERESTED:
                raise InvalidTransitionError("closed conversations cannot be reactivated")
            if state.lifecycle is Lifecycle.ACTIVE:
                return state
            connection.execute(
                """
                UPDATE conversations
                SET lifecycle = 'active', strike_count = 0,
                    followup_pending = 0, activation_epoch = activation_epoch + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (_timestamp(now), conversation_id),
            )
            connection.execute(
                """
                INSERT INTO messages (conversation_id, sender, content, created_at)
                VALUES (?, 'system', ?, ?)
                """,
                (
                    conversation_id,
                    "人工操作员已重新激活 Agent；异常计数已清零，发送限流记录保留。",
                    _timestamp(now),
                ),
            )
            updated = self._require_conversation(connection, conversation_id)
        return _conversation_from_row(updated)

    def list_messages(self, conversation_id: str) -> list[MessageRecord]:
        self.get_conversation(conversation_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, sender, content, request_id, created_at
                FROM messages WHERE conversation_id = ?
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        records: list[MessageRecord] = []
        for row in rows:
            created_at = _datetime(row["created_at"])
            assert created_at is not None
            records.append(
                MessageRecord(
                    id=int(row["id"]),
                    sender=str(row["sender"]),
                    content=str(row["content"]),
                    request_id=(str(row["request_id"]) if row["request_id"] is not None else None),
                    created_at=created_at,
                )
            )
        return records

    def list_turns(self, conversation_id: str) -> list[TurnResult]:
        self.get_conversation(conversation_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT result_json FROM turns WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        return [TurnResult.model_validate_json(row["result_json"]) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, *, immediate: bool) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _require_conversation(connection: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        return cast(sqlite3.Row, row)


def _timestamp(value: datetime) -> float:
    return value.astimezone(UTC).timestamp()


def _datetime(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), UTC)


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        name=row["name"],
        lifecycle=Lifecycle(row["lifecycle"]),
        strike_count=int(row["strike_count"]),
        followup_pending=bool(row["followup_pending"]),
        activation_epoch=int(row["activation_epoch"]),
        last_outbound_at=_datetime(row["last_outbound_at"]),
        created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_datetime(row["updated_at"]),  # type: ignore[arg-type]
    )
