# messenger-mcp

Facebook Messenger as an [MCP](https://modelcontextprotocol.io) server, so that
Claude Code, Codex and other MCP clients can read and send your Messenger
messages.

It is a thin daemon around [fbchat-v2](https://github.com/m008v/fbchat-v2): one
long-running process holds a single Messenger connection (including end-to-end
encrypted chats), logs incoming messages to a local SQLite file, and exposes
tools over Streamable HTTP on `127.0.0.1`. Every MCP client shares that one
connection.

> [!WARNING]
> This uses an **unofficial** API with your **personal account's session
> cookie**. That is against Facebook's terms of service and can get the account
> checkpointed or disabled. The cookie grants full access to the account. Use
> it at your own risk, ideally not with an account you cannot afford to lose.

## Tools

| Tool | What it does |
|---|---|
| `status` | Connection state and the logged-in account id |
| `list_threads` | Recent inbox conversations (names, unread counts, snippets) |
| `get_messages` | Read locally stored messages |
| `wait_for_messages` | Long-poll for new messages (cursor based) |
| `send_message` | Send a text message (E2EE for 1:1 chats, regular for groups) |
| `search_users` | Find a user id by name |

### Limitations

- **No history.** fbchat-v2 has no API to fetch past messages. `get_messages`
  and `wait_for_messages` only return what arrived (or was sent through this
  server) while the daemon was running. Keep it running.
- Text only. Attachments on incoming messages are listed as metadata; nothing
  is downloaded, and sending attachments is not implemented.
- Facebook can change its private endpoints at any time and break this.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Linux, macOS and
Windows (x64) are supported by the prebuilt E2EE bridge that fbchat-v2
downloads (checksum-verified) on first start.

```bash
git clone https://github.com/fukata/messenger-mcp
cd messenger-mcp
uv tool install .
```

### 1. Cookie

Log in to facebook.com in a browser, open DevTools → Application → Cookies, and
copy the values of `c_user`, `xs`, `fr` and `datr` into
`~/.config/messenger-mcp/config.json`:

```bash
mkdir -p ~/.config/messenger-mcp
cat > ~/.config/messenger-mcp/config.json <<'JSON'
{ "cookies": "c_user=...; xs=...; fr=...; datr=...;" }
JSON
chmod 600 ~/.config/messenger-mcp/config.json
```

The file is re-read on every reconnect, so an expired cookie can be replaced
without restarting.

### 2. Run the daemon

```bash
messenger-mcp
```

Or as a systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cp contrib/messenger-mcp.service ~/.config/systemd/user/
systemctl --user enable --now messenger-mcp
journalctl --user -u messenger-mcp -f
```

### 3. Register with your clients

```bash
# Claude Code
claude mcp add --scope user --transport http messenger http://127.0.0.1:8765/mcp

# Codex
codex mcp add messenger --url http://127.0.0.1:8765/mcp
```

Then ask the agent to call `status`; it should report `connected`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MESSENGER_MCP_CONFIG` | `~/.config/messenger-mcp/config.json` | Cookie file |
| `MESSENGER_MCP_DATA_DIR` | `~/.local/share/messenger-mcp` | Message log and E2EE device state |
| `MESSENGER_MCP_HOST` | `127.0.0.1` | Bind address |
| `MESSENGER_MCP_PORT` | `8765` | Port |
| `MESSENGER_MCP_TOKEN` | unset | If set, requests must send `Authorization: Bearer <token>` |
| `FB_COOKIES` | unset | Cookie string; overrides the config file |

With a token:

```bash
claude mcp add --scope user --transport http messenger http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $MESSENGER_MCP_TOKEN"
codex mcp add messenger --url http://127.0.0.1:8765/mcp \
  --bearer-token-env-var MESSENGER_MCP_TOKEN
```

## Security notes

- Without a token, **any local process** that can reach the port can read and
  send your messages. Binding to a non-loopback address requires a token.
- `messages.db` contains decrypted message text in plain text, and
  `e2ee_device.json` contains your E2EE device keys. Both live in the data
  directory with owner-only permissions; treat them like the cookie.
- Incoming message text is untrusted input to the agent reading it. Keep
  `send_message` behind your client's permission prompt rather than
  auto-approving it.

## Development

```bash
uv sync
uv run pytest
```
