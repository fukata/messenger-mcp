import asyncio
import json
import threading

import pytest

from messenger_mcp.config import Config
from messenger_mcp.server import build_server
from messenger_mcp.service import MessengerService, NotConnectedError, parse_thread_list
from messenger_mcp.store import MessageStore

SELF_ID = "100000000000001"


class FakeListener:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self._stop = threading.Event()
        self.callback = None
        self._bridge = None

    def on_message(self, fn):
        self.callback = fn

    async def connect_mqtt(self) -> None:
        await asyncio.to_thread(self._stop.wait)

    def wait_until_connected(self, timeout, *, require_e2ee=False) -> bool:
        return True

    def stop(self) -> None:
        self._stop.set()

    async def send_e2ee_message(self, jid, text, reply_to_id="", reply_to_sender_jid=""):
        self.sent.append(("e2ee", jid, text, reply_to_id, reply_to_sender_jid))
        return {"messageId": f"out-{len(self.sent)}", "timestampMs": 5}

    async def send_message(self, thread_id, text, reply_to_id=""):
        self.sent.append(("regular", thread_id, text, reply_to_id))
        return {"messageId": f"out-{len(self.sent)}", "timestampMs": 5}


@pytest.fixture
def service(tmp_path):
    config = Config(tmp_path / "config.json", tmp_path, "127.0.0.1", 0, None)
    store = MessageStore(":memory:")
    service = MessengerService(config, store)
    service._listener = FakeListener()
    service._data_fb = {"FacebookID": SELF_ID}
    service.state = "connected"
    return service


def e2ee_event(message_id="m1", sender="200", text="hi"):
    return {
        "type": "e2eeMessage",
        "data": {
            "id": message_id,
            "threadId": 200,
            "chatJid": "200@msgr",
            "senderJid": f"{sender}@msgr",
            "senderId": int(sender),
            "text": text,
            "timestampMs": 1,
            "attachments": [{"type": "image", "mediaKey": "c2VjcmV0", "mimeType": "image/png"}],
        },
    }


def test_incoming_message_is_stored_once_without_media_keys(service):
    service.handle_event(e2ee_event())
    service.handle_event(e2ee_event())
    [message] = service._store.query()
    assert message["text"] == "hi" and message["encrypted"] and not message["is_self"]
    assert message["attachments"] == [{"type": "image", "mimeType": "image/png"}]


def test_edit_and_unsend(service):
    service.handle_event(e2ee_event())
    service.handle_event({"type": "messageEdit", "data": {"messageId": "m1", "newText": "yo"}})
    assert service._store.get("m1")["text"] == "yo"
    service.handle_event({"type": "messageUnsend", "data": {"messageId": "m1"}})
    assert service._store.get("m1")["unsent"] and service._store.get("m1")["text"] == ""


async def test_send_routes_by_thread_kind(service):
    listener = service._listener
    service.handle_event(e2ee_event())
    service.handle_event(
        {"type": "message", "data": {"id": "g1", "threadId": 900, "threadType": 2,
                                     "senderId": 200, "text": "group"}}
    )
    await service.send("200", "reply", reply_to="m1")
    await service.send("900", "to group")
    await service.send("300", "unknown user")
    assert listener.sent == [
        ("e2ee", "200@msgr", "reply", "m1", "200@msgr"),
        ("regular", 900, "to group", ""),
        ("e2ee", "300@msgr", "unknown user", "", ""),
    ]
    own = service._store.query(thread_id="200")[-1]
    assert own["is_self"] and own["text"] == "reply"


async def test_send_requires_connection_and_text(service):
    with pytest.raises(ValueError):
        await service.send("200", "  ")
    service.state = "reconnecting"
    with pytest.raises(NotConnectedError):
        await service.send("200", "x")


async def test_wait_for_messages_wakes_on_new_incoming(service):
    mcp = build_server(service, service._store)
    asyncio.get_running_loop().call_later(0.05, service.handle_event, e2ee_event("m2"))
    result = await mcp.call_tool("wait_for_messages", {"timeout_seconds": 5})
    payload = result.structured_content if hasattr(result, "structured_content") else result
    assert json.dumps(payload, default=str).count('"m2"') >= 1


async def test_run_connects_and_stops(service, monkeypatch):
    import fbchat_v2._core._session as session

    async def fake_home(cookies):
        return {"FacebookID": SELF_ID}

    async def no_threads():
        return []

    monkeypatch.setattr(session, "dataGetHome", fake_home)
    monkeypatch.setenv("FB_COOKIES", "c_user=1; xs=2; fr=3; datr=4;")
    service._listener, service.state = None, "starting"
    service._listener_factory = lambda data_fb: FakeListener()
    service.list_threads = no_threads
    task = asyncio.create_task(service.run())
    for _ in range(100):
        if service.state == "connected":
            break
        await asyncio.sleep(0.02)
    assert service.state == "connected"
    await service.stop()
    await asyncio.wait_for(task, 5)
    assert service.state == "stopped"


def test_parse_thread_list_keeps_one_to_one_threads():
    nodes = [
        {"thread_key": {"thread_fbid": None, "other_user_id": "200"}, "name": None,
         "unread_count": 2, "updated_time_precise": "1700000000000",
         "last_message": {"nodes": [{"snippet": "hey"}]},
         "all_participants": {"edges": [
             {"node": {"messaging_actor": {"id": SELF_ID, "name": "Me"}}},
             {"node": {"messaging_actor": {"id": "200", "name": "Alice"}}}]}},
        {"thread_key": {"thread_fbid": "900", "other_user_id": None}, "name": "Team"},
    ]
    data = json.dumps({"o0": {"data": {"viewer": {"message_threads": {"nodes": nodes}}}}})
    one, group = parse_thread_list(data, SELF_ID)
    assert (one["thread_id"], one["name"], one["is_group"], one["unread_count"]) == ("200", "Alice", False, 2)
    assert (group["thread_id"], group["is_group"]) == ("900", True)
    assert parse_thread_list("not json", SELF_ID) == []


async def test_tool_errors_reach_the_client(service):
    from mcp.server.mcpserver.exceptions import ToolError

    service.state, service.last_error = "auth_error", "cookie expired"
    mcp = build_server(service, service._store)
    with pytest.raises(ToolError, match="Messenger is auth_error \\(cookie expired\\)"):
        await mcp.call_tool("send_message", {"thread_id": "200", "text": "x"})


def test_bridge_cookies_do_not_require_fr(service):
    make = lambda cookies: service._make_listener(
        {"FacebookID": SELF_ID, "cookieFacebook": cookies}
    )._build_cookie_dict()
    assert make("c_user=1; xs=2; datr=3; sb=4; ps_l=1") == {
        "c_user": "1", "xs": "2", "datr": "3", "sb": "4"
    }
    with pytest.raises(ValueError, match="xs"):
        make("c_user=1; datr=3")


def test_import_export_is_idempotent_and_keeps_history_in_place(tmp_path):
    import zipfile

    from messenger_mcp.importer import import_thread, read_export

    export = {
        "participants": ["Alice", "Me"],
        "threadName": "Alice_0",
        "messages": [
            {"senderName": "Alice", "text": "old", "timestamp": 1000, "type": "text",
             "isUnsent": False, "media": [], "reactions": []},
            {"senderName": "Me", "text": "pic", "timestamp": 2000, "type": "media",
             "isUnsent": False, "media": [{"uri": "media/a.jpeg"}], "reactions": []},
            {"senderName": "Alice", "text": "new", "timestamp": 9001, "type": "text",
             "isUnsent": False, "media": [], "reactions": []},
            {"senderName": "Alice", "text": "", "timestamp": 3000, "type": "media",
             "isUnsent": False, "media": [{"uri": "media/b.jpeg"}], "reactions": []},
            {"senderName": "Alice", "text": "", "timestamp": 3000, "type": "media",
             "isUnsent": False, "media": [{"uri": "media/c.jpeg"}], "reactions": []},
        ],
    }
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Alice_0.json", json.dumps(export))
        archive.writestr("media/a.jpeg", b"x")
    store = MessageStore(tmp_path / "m.db")
    live = store.add(message_id="live", thread_id="200", sender_id="200",
                     text="new", timestamp_ms=9000)
    [thread] = read_export(path)
    # "new" was already captured live; the two same-instant photos are distinct.
    assert import_thread(store, thread, "200", SELF_ID) == (4, 1)
    assert import_thread(store, thread, "200", SELF_ID) == (0, 5)

    messages = store.query(thread_id="200")
    assert [m["text"] for m in messages] == ["old", "pic", "", "", "new"]
    assert [m["is_self"] for m in messages] == [False, True, False, False, False]
    assert messages[0]["sender_name"] == "Alice" and messages[0]["encrypted"]
    assert messages[1]["attachments"] == [{"type": "media", "fileName": "media/a.jpeg"}]
    # Imported history must not look like newly arrived messages.
    assert store.query(after_seq=live, live_only=True) == []


def test_store_migrates_old_schema(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " message_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL, chat_jid TEXT,"
        " thread_name TEXT, sender_id TEXT NOT NULL, sender_jid TEXT,"
        " is_self INTEGER NOT NULL DEFAULT 0, encrypted INTEGER NOT NULL DEFAULT 0,"
        " text TEXT NOT NULL DEFAULT '', attachments TEXT NOT NULL DEFAULT '[]',"
        " reply_to_id TEXT, timestamp_ms INTEGER NOT NULL DEFAULT 0,"
        " edited INTEGER NOT NULL DEFAULT 0, unsent INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute("INSERT INTO messages (message_id, thread_id, sender_id) VALUES ('a', '1', '1')")
    db.commit(); db.close()
    store = MessageStore(path)
    assert store.add(message_id="b", thread_id="1", sender_id="1", imported=True)
    assert len(store.query(thread_id="1")) == 2


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params):
        self.calls.append((method, params))
        return {"messageId": f"file-{len(self.calls)}", "timestampMs": 7}


async def test_send_file_picks_the_bridge_call_by_type(service, tmp_path, monkeypatch):
    import base64

    monkeypatch.setenv("MESSENGER_MCP_SEND_DIRS", str(tmp_path))
    bridge = service._listener._bridge = FakeBridge()
    (tmp_path / "photo.png").write_bytes(b"png-bytes")
    (tmp_path / "notes.pdf").write_bytes(b"pdf-bytes")

    await service.send_file("200", str(tmp_path / "photo.png"), caption="look")
    result = await service.send_file("200", str(tmp_path / "notes.pdf"), caption="fyi")

    (image_method, image), (doc_method, doc) = bridge.calls
    assert image_method == "sendE2EEImage" and doc_method == "sendE2EEDocument"
    assert image["chatJid"] == "200@msgr" and image["caption"] == "look"
    assert base64.b64decode(image["data"]) == b"png-bytes" and image["mimeType"] == "image/png"
    assert doc["filename"] == "notes.pdf" and "caption" not in doc
    # A document has no caption field, so the caption goes out as text.
    assert service._listener.sent == [("e2ee", "200@msgr", "fyi", "", "")]
    assert result["caption_message"]["mode"] == "e2ee"
    stored = service._store.get("file-1")
    assert stored["text"] == "look" and stored["attachments"][0]["fileName"] == "photo.png"


async def test_send_file_refuses_groups_and_unsafe_paths(service, tmp_path, monkeypatch):
    service._listener._bridge = FakeBridge()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MESSENGER_MCP_SEND_DIRS", raising=False)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_bytes(b"key")
    (tmp_path / "ok.txt").write_bytes(b"hello")
    (tmp_path / "empty.txt").write_bytes(b"")
    service._groups.add("900")

    with pytest.raises(ValueError, match="hidden path"):
        await service.send_file("200", str(tmp_path / ".ssh" / "id_rsa"))
    with pytest.raises(ValueError, match="outside your home"):
        await service.send_file("200", "/etc/hostname")
    with pytest.raises(ValueError, match="1:1"):
        await service.send_file("900", str(tmp_path / "ok.txt"))
    with pytest.raises(ValueError, match="1 byte"):
        await service.send_file("200", str(tmp_path / "empty.txt"))
    assert service._listener._bridge.calls == []
    assert (await service.send_file("200", str(tmp_path / "ok.txt")))["kind"] == "file"
