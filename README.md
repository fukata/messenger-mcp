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
| `send_file` | Send a local image, video, audio or other file to a 1:1 chat |
| `search_users` | Find a user id by name |

### Limitations

- **No history.** fbchat-v2 has no API to fetch past messages. `get_messages`
  and `wait_for_messages` only return what arrived (or was sent through this
  server) while the daemon was running. Keep it running. Older messages can be
  [imported from an export](#importing-past-messages).
- Attachments on incoming messages are listed as metadata; nothing is
  downloaded. Files can be sent to 1:1 chats only, not to groups.
- Facebook can change its private endpoints at any time and break this.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Linux, macOS and
Windows should work, but only Linux has been tested.

```bash
git clone https://github.com/fukata/messenger-mcp
cd messenger-mcp
uv tool install .
```

### 1. Build the bridge

The prebuilt E2EE bridge that fbchat-v2 2.3.2 downloads is built against an
old mautrix-meta and can no longer connect (`regular connection did not become
ready`). Build a current one instead (needs git and Go; takes a few minutes):

```bash
./scripts/build-bridge.sh
```

It is installed to `~/.local/share/messenger-mcp/bin/` and picked up
automatically. Regular (non-E2EE) media upload is disabled in this build; the
server does not use it.

### 2. Cookie

Log in to facebook.com in a browser, open DevTools → Application → Cookies, and
copy the values of `c_user`, `xs` and `datr` (plus `fr` and `sb` if present) into
`~/.config/messenger-mcp/config.json`:

```bash
mkdir -p ~/.config/messenger-mcp
cat > ~/.config/messenger-mcp/config.json <<'JSON'
{ "cookies": "c_user=...; xs=...; datr=...; sb=...;" }
JSON
chmod 600 ~/.config/messenger-mcp/config.json
```

The file is re-read on every reconnect, so an expired cookie can be replaced
without restarting.

### 3. Run the daemon

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

### 4. Register with your clients

```bash
# Claude Code
claude mcp add --scope user --transport http messenger http://127.0.0.1:8765/mcp

# Codex
codex mcp add messenger --url http://127.0.0.1:8765/mcp
```

Then ask the agent to call `status`; it should report `connected`.

## Importing past messages

Past end-to-end encrypted messages cannot be fetched through the API, but
Messenger can export them: on messenger.com (desktop), open *Privacy & safety →
End-to-end encrypted chats → Message storage → Download secure storage data*.

The export contains names only, so tell the importer which thread id each
conversation belongs to (`search_users` or `list_threads` gives you the id):

```bash
messenger-mcp import messages.zip                      # list conversations
messenger-mcp import messages.zip --thread "Jane Doe=100012345678"
```

Importing is idempotent, skips messages the daemon already captured live, and
does not wake `wait_for_messages`. Media files are listed by name only.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MESSENGER_MCP_CONFIG` | `~/.config/messenger-mcp/config.json` | Cookie file |
| `MESSENGER_MCP_DATA_DIR` | `~/.local/share/messenger-mcp` | Message log and E2EE device state |
| `MESSENGER_MCP_HOST` | `127.0.0.1` | Bind address |
| `MESSENGER_MCP_PORT` | `8765` | Port |
| `MESSENGER_MCP_TOKEN` | unset | If set, requests must send `Authorization: Bearer <token>` |
| `MESSENGER_MCP_SEND_DIRS` | home directory, excluding hidden paths | Directories `send_file` may read from (`:`-separated) |
| `MESSENGER_MCP_BRIDGE` | `<data dir>/bin/fbchat-bridge-e2ee` if present | Path to the bridge binary |
| `FB_COOKIES` | unset | Cookie string; overrides the config file |
| `MESSENGER_MCP_DEBUG` | unset | Set to `1` to log bridge output (verbose; includes message metadata) |

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
  `send_message` and `send_file` behind your client's permission prompt rather
  than auto-approving them. `send_file` refuses files outside your home
  directory and anything under a dot-directory (`~/.ssh`, `~/.config`, ...)
  unless `MESSENGER_MCP_SEND_DIRS` says otherwise.

## Development

```bash
uv sync
uv run pytest
```

## License

[MIT](LICENSE). fbchat-v2 and the E2EE bridge it downloads at runtime are
separate projects under their own licenses.
