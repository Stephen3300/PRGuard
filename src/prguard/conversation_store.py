from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .data import PROJECT_ROOT


DEFAULT_DB_PATH = PROJECT_ROOT / "outputs" / "conversations" / "prguard.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConversationStore:
    """Local SQLite store for PR context and role-by-role chat history."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    issue TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    tone_mode TEXT NOT NULL DEFAULT '中性专业',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    speaker_type TEXT NOT NULL CHECK (speaker_type IN ('user', 'assistant', 'system')),
                    speaker_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    validation_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                    UNIQUE (conversation_id, sequence_no)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_sequence
                    ON messages(conversation_id, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
                    ON conversations(updated_at DESC);
                """
            )

    def create_conversation(
        self,
        issue: str,
        diff: str,
        *,
        title: str | None = None,
        tone_mode: str = "中性专业",
    ) -> str:
        first_line = next(
            (line.strip() for line in (title or issue).splitlines() if line.strip()),
            "PR 评审对话",
        )
        conversation_id = uuid.uuid4().hex[:16]
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO conversations
                   (id, title, issue, diff, tone_mode, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (conversation_id, first_line[:72], issue, diff, tone_mode, now, now),
            )
        return conversation_id

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT c.id, c.title, c.updated_at,
                          COUNT(m.id) AS message_count
                   FROM conversations AS c
                   LEFT JOIN messages AS m ON m.conversation_id = c.id
                   GROUP BY c.id
                   ORDER BY c.updated_at DESC
                   LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_tone(self, conversation_id: str, tone_mode: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET tone_mode = ?, updated_at = ? WHERE id = ?",
                (tone_mode, _now(), conversation_id),
            )

    def append_message(
        self,
        conversation_id: str,
        *,
        speaker_type: str,
        speaker_id: str,
        content: str,
        validation: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if speaker_type not in {"user", "assistant", "system"}:
            raise ValueError(f"Unknown speaker type: {speaker_type}")
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), -1) + 1 AS next_no FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            sequence_no = int(row["next_no"])
            cursor = connection.execute(
                """INSERT INTO messages
                   (conversation_id, sequence_no, speaker_type, speaker_id,
                    content, validation_json, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    sequence_no,
                    speaker_type,
                    speaker_id,
                    content,
                    json.dumps(validation, ensure_ascii=False) if validation is not None else None,
                    json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                    now,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return int(cursor.lastrowid)

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sequence_no",
                (conversation_id,),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["validation"] = json.loads(item.pop("validation_json")) if item.get("validation_json") else None
            item["metadata"] = json.loads(item.pop("metadata_json")) if item.get("metadata_json") else None
            messages.append(item)
        return messages

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

