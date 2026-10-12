"""Owns the single Messenger connection and keeps it alive."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Callable

from .config import Config
from .store import MessageStore, clean_attachments

logger = logging.getLogger("messenger_mcp")

CONNECT_TIMEOUT = 120.0
RETRY_MIN = 5.0
RETRY_MAX = 300.0
# A dead cookie will not fix itself; do not hammer Facebook's login page.
AUTH_RETRY = 600.0
REQUIRED_COOKIES = ("c_user", "xs", "datr")


class NotConnectedError(RuntimeError):
    pass


def parse_thread_list(data_get: str, self_id: str) -> list[dict[str, Any]]:
    """Flatten the raw GraphQL inbox payload returned by fbchat-v2.

    The library's own summary drops 1:1 threads (they have no `thread_fbid`),
    so the nodes are read directly and defensively.
    """
    try:
        root = json.loads(data_get)["o0"]
        nodes = root["data"]["viewer"]["message_threads"]["nodes"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []

    threads = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        key = node.get("thread_key") or {}
        group_id = key.get("thread_fbid")
        thread_id = group_id or key.get("other_user_id")
        if not thread_id:
            continue
        participants = []
        for edge in (node.get("all_participants") or {}).get("edges") or []:
            actor = ((edge or {}).get("node") or {}).get("messaging_actor") or {}
            if actor.get("id"):
                participants.append(
                    {"id": str(actor["id"]), "name": actor.get("name")}
                )
        name = node.get("name")
        if not name and not group_id:
            name = next(
                (p["name"] for p in participants if p["id"] != self_id), None
            )
        last = ((node.get("last_message") or {}).get("nodes") or [{}])[0] or {}
        threads.append(
            {
                "thread_id": str(thread_id),
                "name": name,
                "is_group": bool(group_id),
                "unread_count": node.get("unread_count") or 0,
                "last_snippet": last.get("snippet"),
                "updated_ms": int(node.get("updated_time_precise") or 0),
                "participants": participants,
            }
        )
    return threads


class MessengerService:
    def __init__(
        self,
        config: Config,
        store: MessageStore,
        *,
        listener_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._listener_factory = listener_factory
        self._listener: Any = None
        self._data_fb: dict[str, Any] | None = None
        self._stopping = asyncio.Event()
        self._new_message = asyncio.Event()
        self._names: dict[str, str] = {}
        self._groups: set[str] = set()
        self.state = "starting"
        self.last_error: str | None = None
        self.connected_since: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle

    @property
    def self_id(self) -> str | None:
        return str(self._data_fb["FacebookID"]) if self._data_fb else None

    async def run(self) -> None:
        """Connect, and reconnect with backoff until `stop()` is called."""
        delay = RETRY_MIN
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                await self._run_once()
            except _AuthError as error:
                self.state, self.last_error = "auth_error", str(error)
                logger.error("%s", error)
                delay = AUTH_RETRY
            except Exception as error:  # noqa: BLE001 - keep the daemon alive
                self.state = "reconnecting"
                self.last_error = f"{type(error).__name__}: {error}"
                logger.warning("Messenger connection lost: %s", self.last_error)
                if time.monotonic() - started > 600:
                    delay = RETRY_MIN
            finally:
                self._listener = None
                self.connected_since = None
            if self._stopping.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)
            delay = min(delay * 2, RETRY_MAX)
        self.state = "stopped"

    async def stop(self) -> None:
        self._stopping.set()
        listener = self._listener
        if listener is not None:
            await asyncio.to_thread(listener.stop)

    async def _run_once(self) -> None:
        from fbchat_v2._core._session import dataGetHome

        cookies = self._config.load_cookies()
        if not cookies:
            raise _AuthError(
                f"No Facebook cookie configured. Put it in {self._config.config_path}"
                ' as {"cookies": "c_user=...; xs=...; datr=...;"}.'
            )
        data_fb = await dataGetHome(cookies)
        if data_fb is None:
            raise _AuthError(
                "Facebook rejected the cookie (expired, checkpointed, or missing "
                "c_user/xs/datr). Refresh it in the config file."
            )
        self._data_fb = data_fb

        listener = self._make_listener(data_fb)
        loop = asyncio.get_running_loop()
        # The bridge calls back from its own thread.
        listener.on_message(
            lambda event: loop.call_soon_threadsafe(self.handle_event, event)
        )
        self._listener = listener
        task = asyncio.create_task(listener.connect_mqtt())
        try:
            ready = await asyncio.to_thread(
                listener.wait_until_connected, CONNECT_TIMEOUT, require_e2ee=True
            )
            if not ready:
                raise TimeoutError("Messenger/E2EE handshake did not complete.")
            self.state, self.last_error = "connected", None
            self.connected_since = time.time()
            logger.info("Connected to Messenger as %s", self.self_id)
            with contextlib.suppress(Exception):
                await self.list_threads()  # warm the name cache
            await task
            raise ConnectionError("Messenger listener exited.")
        finally:
            await asyncio.to_thread(listener.stop)
            with contextlib.suppress(BaseException):
                await task

    def _make_listener(self, data_fb: dict[str, Any]) -> Any:
        if self._listener_factory is not None:
            return self._listener_factory(data_fb)
        from fbchat_v2._core._utils import parse_cookie_string
        from fbchat_v2._messaging._listening_e2ee import listeningE2EEEvent

        class Listener(listeningE2EEEvent):
            def _build_cookie_dict(self) -> dict[str, str]:
                # fbchat-v2 also demands `fr`, an ads cookie that browsers with
                # tracking protection never receive. The bridge (mautrix-meta)
                # only needs these three.
                cookies = parse_cookie_string(self.dataFB["cookieFacebook"])
                missing = [c for c in REQUIRED_COOKIES if not cookies.get(c)]
                if missing:
                    raise ValueError(f"Missing required cookies: {missing}")
                keep = {*REQUIRED_COOKIES, "fr", "sb", "wd", "presence"}
                return {k: v for k, v in cookies.items() if k in keep and v}

        # Persist the E2EE device: a fresh device on every restart cannot
        # decrypt anything sent while the daemon was down.
        return Listener(
            data_fb,
            device_path=str(self._config.device_path),
            e2ee_memory_only=False,
        )

    # ------------------------------------------------------------------
    # Incoming events (always called on the event loop thread)

    def handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event(event)
        except Exception:  # noqa: BLE001 - one bad payload must not kill the feed
            logger.exception("Failed to handle %s event", event.get("type"))

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind in ("message", "e2eeMessage"):
            message_id = data.get("id")
            thread_id = data.get("threadId")
            if not message_id or not thread_id:
                return
            sender_id = str(data.get("senderId") or "")
            thread_id = str(thread_id)
            if kind == "message" and data.get("threadType") == 2:  # GROUP_THREAD
                self._groups.add(thread_id)
            seq = self._store.add(
                message_id=str(message_id),
                thread_id=thread_id,
                chat_jid=data.get("chatJid"),
                thread_name=data.get("threadName") or self._names.get(thread_id),
                sender_id=sender_id,
                sender_jid=data.get("senderJid"),
                is_self=sender_id == self.self_id,
                encrypted=kind == "e2eeMessage",
                text=data.get("text") or "",
                attachments=clean_attachments(data.get("attachments")),
                reply_to_id=(data.get("replyTo") or {}).get("messageId"),
                timestamp_ms=data.get("timestampMs") or 0,
            )
            if seq is not None:
                self._notify()
        elif kind == "messageEdit" and data.get("messageId"):
            self._store.mark_edited(str(data["messageId"]), data.get("newText") or "")
        elif kind == "messageUnsend" and data.get("messageId"):
            self._store.mark_unsent(str(data["messageId"]))

    def _notify(self) -> None:
        waiters, self._new_message = self._new_message, asyncio.Event()
        waiters.set()

    async def wait_for_new(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._new_message.wait(), timeout)

    def with_names(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for message in messages:
            name = self._names.get(message["sender_id"])
            if name:
                message["sender_name"] = name
            if "thread_name" not in message:
                thread_name = self._names.get(message["thread_id"])
                if thread_name:
                    message["thread_name"] = thread_name
        return messages

    # ------------------------------------------------------------------
    # Actions

    def _require(self) -> tuple[Any, dict[str, Any]]:
        if self.state != "connected" or self._listener is None or not self._data_fb:
            detail = f" ({self.last_error})" if self.last_error else ""
            raise NotConnectedError(f"Messenger is {self.state}{detail}.")
        return self._listener, self._data_fb

    def resolve_mode(self, thread_id: str, mode: str) -> str:
        if mode in ("e2ee", "regular"):
            return mode
        if mode != "auto":
            raise ValueError("mode must be 'auto', 'e2ee' or 'regular'.")
        if "@" in thread_id:
            return "e2ee"
        seen = self._store.thread_encrypted(thread_id)
        if seen is not None:
            return "e2ee" if seen else "regular"
        # Personal chats are E2EE by default; groups normally are not.
        return "regular" if thread_id in self._groups else "e2ee"

    async def send(
        self, thread_id: str, text: str, *, mode: str = "auto", reply_to: str = ""
    ) -> dict[str, Any]:
        from fbchat_v2._messaging._send_e2ee import normalize_chat_jid

        listener, _ = self._require()
        thread_id = thread_id.strip()
        if not text.strip():
            raise ValueError("text must not be empty.")
        mode = self.resolve_mode(thread_id, mode)

        chat_jid = None
        if mode == "e2ee":
            chat_jid = normalize_chat_jid(thread_id)
            replied = self._store.get(reply_to) if reply_to else None
            result = await listener.send_e2ee_message(
                chat_jid,
                text,
                reply_to_id=reply_to,
                reply_to_sender_jid=(replied or {}).get("sender_jid") or "",
            )
            store_thread = chat_jid.split("@", 1)[0]
        else:
            if not thread_id.isdigit():
                raise ValueError("A regular thread_id must be numeric.")
            result = await listener.send_message(
                int(thread_id), text, reply_to_id=reply_to
            )
            store_thread = thread_id

        message_id = result.get("messageId")
        if message_id:
            self._store.add(
                message_id=str(message_id),
                thread_id=store_thread,
                chat_jid=chat_jid,
                thread_name=self._names.get(store_thread),
                sender_id=self.self_id or "",
                is_self=True,
                encrypted=mode == "e2ee",
                text=text,
                reply_to_id=reply_to or None,
                timestamp_ms=result.get("timestampMs") or int(time.time() * 1000),
            )
        return {
            "message_id": message_id,
            "thread_id": store_thread,
            "mode": mode,
            "delivery_status": result.get("deliveryStatus") or "unknown",
        }

    async def list_threads(self) -> list[dict[str, Any]]:
        from fbchat_v2._features._thread import _all_thread_data

        _, data_fb = self._require()
        result = await _all_thread_data.func(data_fb)
        threads = parse_thread_list(result.get("dataGet") or "", self.self_id or "")
        for thread in threads:
            if thread["name"]:
                self._names[thread["thread_id"]] = thread["name"]
            if thread["is_group"]:
                self._groups.add(thread["thread_id"])
            for participant in thread["participants"]:
                if participant["name"]:
                    self._names.setdefault(participant["id"], participant["name"])
        return threads

    async def search_users(self, query: str) -> list[dict[str, Any]]:
        from fbchat_v2._features._facebook import _search

        _, data_fb = self._require()
        result = await _search.func(data_fb, query)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(f"Search failed: {(result or {}).get('messages')}")
        users = result.get("searchResultsDict") or []
        if isinstance(users, dict):
            users = list(users.values())
        return [user for user in users if isinstance(user, dict)]


class _AuthError(RuntimeError):
    pass
