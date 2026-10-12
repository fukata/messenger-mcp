"""Runtime configuration: where secrets and state live, and how the server listens."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _xdg(env_var: str, fallback: str) -> Path:
    return Path(os.environ.get(env_var) or (Path.home() / fallback))


@dataclass(frozen=True)
class Config:
    config_path: Path
    data_dir: Path
    host: str
    port: int
    token: str | None
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        config_path = Path(
            os.environ.get("MESSENGER_MCP_CONFIG")
            or _xdg("XDG_CONFIG_HOME", ".config") / "messenger-mcp" / "config.json"
        ).expanduser()
        data_dir = Path(
            os.environ.get("MESSENGER_MCP_DATA_DIR")
            or _xdg("XDG_DATA_HOME", ".local/share") / "messenger-mcp"
        ).expanduser()
        config = cls(
            config_path=config_path,
            data_dir=data_dir,
            host=os.environ.get("MESSENGER_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MESSENGER_MCP_PORT", "8765")),
            token=os.environ.get("MESSENGER_MCP_TOKEN") or None,
            debug=os.environ.get("MESSENGER_MCP_DEBUG") == "1",
        )
        if config.host not in LOOPBACK_HOSTS and not config.token:
            raise SystemExit(
                "MESSENGER_MCP_TOKEN is required when MESSENGER_MCP_HOST is not "
                "a loopback address: this server can read and send your messages."
            )
        return config

    @property
    def db_path(self) -> Path:
        return self.data_dir / "messages.db"

    @property
    def device_path(self) -> Path:
        return self.data_dir / "e2ee_device.json"

    @property
    def bridge_path(self) -> Path | None:
        """A locally built bridge (scripts/build-bridge.sh), if there is one.
        Otherwise fbchat-v2 downloads its own prebuilt binary."""
        override = os.environ.get("MESSENGER_MCP_BRIDGE")
        if override:
            return Path(override).expanduser()
        local = self.data_dir / "bin" / "fbchat-bridge-e2ee"
        return local if local.is_file() else None

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)

    def load_cookies(self) -> str | None:
        """Read the Facebook cookie string. Re-read on every (re)connect so a
        refreshed cookie is picked up without restarting the daemon."""
        from_env = os.environ.get("FB_COOKIES")
        if from_env and from_env.strip():
            return from_env.strip()
        try:
            mode = self.config_path.stat().st_mode
        except FileNotFoundError:
            return None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            os.chmod(self.config_path, 0o600)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, str) or not cookies.strip():
            return None
        return cookies.strip()
