"""SQLite log of messages seen by the daemon.

fbchat-v2 has no history API: messages only exist for us if the listener was
running when they arrived, so everything is persisted here. `seq` is a
monotonically increasing cursor used by `wait_for_messages`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL UNIQUE,
    thread_id     TEXT NOT NULL,
    chat_jid      TEXT,
    thread_name   TEXT,
    sender_id     TEXT NOT NULL,
    sender_jid    TEXT,
    is_self       INTEGER NOT NULL DEFAULT 0,
    encrypted     INTEGER NOT NULL DEFAULT 0,
    text          TEXT NOT NULL DEFAULT '',
    attachments   TEXT NOT NULL DEFAULT '[]',
    reply_to_id   TEXT,
    timestamp_ms  INTEGER NOT NULL DEFAULT 0,
    edited        INTEGER NOT NULL DEFAULT 0,
    unsent        INTEGER NOT NULL DEFAULT 0,
    sender_name   TEXT,
    imported      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(thread_id, timestamp_ms);
"""

# Columns added after the first release, for databases created before them.
_MIGRATIONS = {
    "sender_name": "ALTER TABLE messages ADD COLUMN sender_name TEXT",
    "imported": "ALTER TABLE messages ADD COLUMN imported INTEGER NOT NULL DEFAULT 0",
}

# Decryption material for E2EE media must not be handed to the model.
_ATTACHMENT_KEYS = (
    "type",
    "url",
    "previewUrl",
    "fileName",
    "mimeType",
    "fileSize",
    "duration",
    "description",
    "sourceText",
    "latitude",
    "longitude",
)


def clean_attachments(raw: Any) -> list[dict[str, Any]]:
    cleaned = []
    for item in raw or []:
        if isinstance(item, dict):
            cleaned.append({k: item[k] for k in _ATTACHMENT_KEYS if item.get(k)})
    return cleaned


class MessageStore:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            os.chmod(path, 0o600)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}
        if columns:
            for column, statement in _MIGRATIONS.items():
                if column not in columns:
                    self._db.execute(statement)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(
        self,
        *,
        message_id: str,
        thread_id: str,
        sender_id: str,
        text: str = "",
        chat_jid: str | None = None,
        thread_name: str | None = None,
        sender_jid: str | None = None,
        is_self: bool = False,
        encrypted: bool = False,
        attachments: list[dict[str, Any]] | None = None,
        reply_to_id: str | None = None,
        timestamp_ms: int = 0,
        sender_name: str | None = None,
        imported: bool = False,
        unsent: bool = False,
    ) -> int | None:
        """Insert a message. Returns its seq, or None if it was already stored."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO messages (message_id, thread_id, chat_jid,"
            " thread_name, sender_id, sender_jid, is_self, encrypted, text,"
            " attachments, reply_to_id, timestamp_ms, sender_name, imported, unsent)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                thread_id,
                chat_jid,
                thread_name or None,
                sender_id,
                sender_jid,
                int(is_self),
                int(encrypted),
                text or "",
                json.dumps(attachments or [], ensure_ascii=False),
                reply_to_id,
                int(timestamp_ms or 0),
                sender_name,
                int(imported),
                int(unsent),
            ),
        )
        self._db.commit()
        return cursor.lastrowid if cursor.rowcount else None

    def mark_edited(self, message_id: str, new_text: str) -> None:
        self._db.execute(
            "UPDATE messages SET text = ?, edited = 1 WHERE message_id = ?",
            (new_text, message_id),
        )
        self._db.commit()

    def mark_unsent(self, message_id: str) -> None:
        self._db.execute(
            "UPDATE messages SET unsent = 1, text = '', attachments = '[]'"
            " WHERE message_id = ?",
            (message_id,),
        )
        self._db.commit()

    def get(self, message_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return self._row(row) if row else None

    def has_similar(self, thread_id: str, text: str, timestamp_ms: int) -> bool:
        """Whether the daemon captured the same text live at (nearly) the same
        time. Exports carry no message ids, so this is how an import avoids
        duplicating those; imported rows are deduplicated by their own ids."""
        row = self._db.execute(
            "SELECT 1 FROM messages WHERE thread_id = ? AND text = ? AND imported = 0"
            " AND ABS(timestamp_ms - ?) < 5000 LIMIT 1",
            (thread_id, text or "", int(timestamp_ms)),
        ).fetchone()
        return row is not None

    def max_seq(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()
        return int(row[0])

    def thread_encrypted(self, thread_id: str) -> bool | None:
        """Whether the most recent message seen in a thread was E2EE."""
        row = self._db.execute(
            "SELECT encrypted FROM messages WHERE thread_id = ?"
            " ORDER BY seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return bool(row[0]) if row else None

    def query(
        self,
        *,
        thread_id: str | None = None,
        after_seq: int | None = None,
        before_ms: int | None = None,
        incoming_only: bool = False,
        live_only: bool = False,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Messages, oldest first.

        With `after_seq`: the next messages in arrival order (the cursor used by
        wait_for_messages). Otherwise: the newest messages by send time, which
        keeps imported history in its place."""
        where, params = ["1=1"], []
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        if after_seq is not None:
            where.append("seq > ?")
            params.append(after_seq)
        if before_ms is not None:
            where.append("timestamp_ms < ?")
            params.append(before_ms)
        if incoming_only:
            where.append("is_self = 0")
        if live_only:
            where.append("imported = 0")
        order = "seq ASC" if after_seq is not None else "timestamp_ms DESC, seq DESC"
        rows = self._db.execute(
            f"SELECT * FROM messages WHERE {' AND '.join(where)}"
            f" ORDER BY {order} LIMIT ?",
            (*params, limit),
        ).fetchall()
        messages = [self._row(row) for row in rows]
        return messages if after_seq is not None else messages[::-1]

    def threads(self, limit: int = 50) -> list[dict[str, Any]]:
        """Threads that have locally stored messages, most recently active first."""
        rows = self._db.execute(
            "SELECT m.* , c.count, c.incoming FROM messages m JOIN ("
            "  SELECT MAX(seq) AS seq, COUNT(*) AS count,"
            "         SUM(is_self = 0) AS incoming"
            "  FROM messages GROUP BY thread_id"
            ") c ON c.seq = m.seq ORDER BY m.seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "thread_id": row["thread_id"],
                "name": row["thread_name"],
                "encrypted": bool(row["encrypted"]),
                "stored_messages": row["count"],
                "last_message": self._row(row),
            }
            for row in rows
        ]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        message = {
            "seq": row["seq"],
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "sender_id": row["sender_id"],
            "is_self": bool(row["is_self"]),
            "encrypted": bool(row["encrypted"]),
            "text": row["text"],
            "timestamp_ms": row["timestamp_ms"],
        }
        if row["thread_name"]:
            message["thread_name"] = row["thread_name"]
        if row["sender_name"]:
            message["sender_name"] = row["sender_name"]
        if row["sender_jid"]:
            message["sender_jid"] = row["sender_jid"]
        attachments = json.loads(row["attachments"])
        if attachments:
            message["attachments"] = attachments
        if row["reply_to_id"]:
            message["reply_to_id"] = row["reply_to_id"]
        if row["edited"]:
            message["edited"] = True
        if row["unsent"]:
            message["unsent"] = True
        return message
