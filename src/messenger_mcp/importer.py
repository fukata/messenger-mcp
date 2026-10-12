"""Import Messenger's "Download secure storage data" export into the message log.

The export is a zip of one JSON file per conversation:

    {"participants": [names], "threadName": "<name>_<n>",
     "messages": [{"senderName", "text", "timestamp" (ms), "type",
                   "isUnsent", "media": [{"uri"}], "reactions": [...]}]}

It carries names only - no thread, user or message ids - so the caller maps
each conversation to its thread id, and message ids are derived from content.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import MessageStore


@dataclass
class ThreadExport:
    name: str
    participants: list[str]
    messages: list[dict[str, Any]]


def read_export(path: Path | str) -> list[ThreadExport]:
    threads = []
    with zipfile.ZipFile(path) as archive:
        for entry in archive.namelist():
            if not entry.endswith(".json"):
                continue
            data = json.loads(archive.read(entry))
            if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                continue
            threads.append(
                ThreadExport(
                    # threadName carries the export's "_<n>" file suffix.
                    name=re.sub(r"_\d+$", "", str(data.get("threadName") or "")),
                    participants=[str(p) for p in data.get("participants") or []],
                    messages=[m for m in data["messages"] if isinstance(m, dict)],
                )
            )
    return threads


def _self_name(thread: ThreadExport) -> str | None:
    """In a 1:1 export the thread is named after the other person."""
    others = [p for p in thread.participants if p != thread.name]
    return others[0] if len(thread.participants) == 2 and len(others) == 1 else None


def import_thread(
    store: MessageStore, thread: ThreadExport, thread_id: str, self_id: str
) -> tuple[int, int]:
    """Store one conversation. Returns (imported, skipped as already present)."""
    self_name = _self_name(thread)
    one_to_one = self_name is not None
    imported = skipped = 0
    seen: dict[tuple[int, str, str], int] = {}
    for message in thread.messages:
        timestamp_ms = int(message.get("timestamp") or 0)
        sender = str(message.get("senderName") or "")
        text = str(message.get("text") or "")
        is_self = sender == self_name
        # Identical messages in the same millisecond still need distinct ids.
        repeat = seen.get((timestamp_ms, sender, text), 0)
        seen[(timestamp_ms, sender, text)] = repeat + 1
        digest = hashlib.sha256(
            f"{thread_id}\0{timestamp_ms}\0{sender}\0{text}\0{repeat or ''}".encode()
        ).hexdigest()[:24]
        message_id = f"import:{digest}"
        if store.get(message_id) or store.has_similar(thread_id, text, timestamp_ms):
            skipped += 1
            continue
        attachments = [
            {"type": str(message.get("type") or "media"), "fileName": str(m["uri"])}
            for m in message.get("media") or []
            if isinstance(m, dict) and m.get("uri")
        ]
        store.add(
            message_id=message_id,
            thread_id=thread_id,
            chat_jid=f"{thread_id}@msgr" if one_to_one else None,
            thread_name=thread.name,
            sender_id=self_id if is_self else (thread_id if one_to_one else ""),
            sender_name=sender or None,
            is_self=is_self,
            encrypted=True,
            text=text,
            attachments=attachments,
            timestamp_ms=timestamp_ms,
            imported=True,
            unsent=bool(message.get("isUnsent")),
        )
        imported += 1
    return imported, skipped
