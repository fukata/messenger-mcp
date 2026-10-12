"""Facebook Messenger as an MCP server (single shared daemon, Streamable HTTP)."""

from __future__ import annotations

import asyncio
import hmac
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config.from_env()
    if config.host not in LOOPBACK_HOSTS:
        logging.getLogger("messenger_mcp").warning(
            "Listening on %s: anyone with the token can use your account.", config.host
        )
    asyncio.run(serve(config))
