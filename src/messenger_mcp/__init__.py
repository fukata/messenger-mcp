"""Facebook Messenger as an MCP server (single shared daemon, Streamable HTTP)."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
from typing import Any

import uvicorn

from .config import LOOPBACK_HOSTS, Config
from .server import build_server
from .service import MessengerService
from .store import MessageStore


class BearerAuth:
    """Reject HTTP requests that do not carry the configured bearer token."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self._expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self._app(scope, receive, send)


async def serve(config: Config) -> None:
    config.ensure_data_dir()
    store = MessageStore(config.db_path)
    service = MessengerService(config, store)
    # Stateless: clients survive daemon restarts without a stale session id.
    app: Any = build_server(service, store).streamable_http_app(
        stateless_http=True, json_response=True, host=config.host
    )
    if config.token:
        app = BearerAuth(app, config.token)

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="info")
    )
    connection = asyncio.create_task(service.run())
    try:
        await server.serve()
    finally:
        await service.stop()
        await connection
        store.close()


def _import(config: Config, args: argparse.Namespace) -> None:
    from .importer import import_thread, read_export

    mapping = dict(item.split("=", 1) for item in args.thread)
    threads = read_export(args.zip)
    if not mapping:
        print("Conversations in this export (pass --thread 'NAME=THREAD_ID' to import):")
        for thread in threads:
            print(f"  {len(thread.messages):6d} messages  {thread.name}")
        return
    unknown = set(mapping) - {thread.name for thread in threads}
    if unknown:
        raise SystemExit(f"Not in this export: {sorted(unknown)}")
    try:
        device = json.loads(config.device_path.read_text(encoding="utf-8"))
        self_id = str(device.get("jid_user") or "")
    except (OSError, ValueError):
        self_id = ""
    self_id = args.self_id or self_id
    if not self_id:
        raise SystemExit("Could not determine your own user id; pass --self-id.")
    config.ensure_data_dir()
    store = MessageStore(config.db_path)
    for thread in threads:
        if thread.name in mapping:
            imported, skipped = import_thread(
                store, thread, mapping[thread.name].strip(), self_id
            )
            print(f"{thread.name}: imported {imported}, already present {skipped}")
    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="messenger-mcp")
    commands = parser.add_subparsers(dest="command")
    importer = commands.add_parser(
        "import", help="import a Messenger 'secure storage data' export (zip)"
    )
    importer.add_argument("zip")
    importer.add_argument(
        "--thread",
        action="append",
        default=[],
        metavar="NAME=THREAD_ID",
        help="conversation name in the export and its thread id (repeatable)",
    )
    importer.add_argument("--self-id", help="your own Facebook user id")
    args = parser.parse_args()
    if args.command == "import":
        _import(Config.from_env(), args)
        return

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config.from_env()
    if config.host not in LOOPBACK_HOSTS:
        logging.getLogger("messenger_mcp").warning(
            "Listening on %s: anyone with the token can use your account.", config.host
        )
    asyncio.run(serve(config))
