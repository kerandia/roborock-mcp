# Roborock MCP server

A standalone stdio Model Context Protocol server wrapping
[python-roborock](https://github.com/humbertogontijo/python-roborock).
It exposes vacuum status, rooms, cleaning history, consumables and robot controls
to a trusted MCP client.

Extracted from a local Roborock project. This repository does **not** include its
Apple Intelligence assistant, Telegram bot, conversation memory, personal
configuration, credentials, or voice/firmware experiments.

The original integration was tested with python-roborock 5.36.0 on a Roborock
Qrevo C. Other V1-protocol models may behave differently. Dependencies are pinned
to the versions used for this extraction; Python 3.14 was used for verification.

## Setup

Run from this repository's directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# The login helper writes credentials to disk. Restrict newly created files.
umask 077
export ROBOROCK_USERNAME="you@example.com"
.venv/bin/python authenticate.py
chmod 600 roborock_user_data.json
```

The helper requests an email login code and writes long-lived account tokens to
`roborock_user_data.json`. Never commit or share that file. The MCP server reads
these tokens; it does not perform interactive login itself.

Register this server in your MCP client's configuration. Adapt the enclosing
configuration format to that client, and replace all paths with absolute paths:

```json
{
  "mcpServers": {
    "roborock": {
      "command": "/absolute/path/to/roborock-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/roborock-mcp/roborock_mcp_server.py"],
      "env": {
        "ROBOROCK_USERNAME": "you@example.com",
        "ROBOROCK_USER_DATA": "/absolute/path/to/roborock_user_data.json"
      }
    }
  }
}
```

Optional `ROBOROCK_DEVICE` selects an exact device ID or case-insensitive name
substring. **Current behavior:** with no selector, or a selector that matches
nothing, the server uses the first compatible vacuum. Verify the selected device
before enabling control tools, especially on multi-vacuum accounts.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_status` | Activity, battery, errors and model-specific fan settings |
| `list_rooms` | Names and segment IDs from the current map |
| `get_consumables` | Consumable wear data |
| `get_clean_history` | Summary and up to 50 detailed recent records |
| `start_clean` | Start whole-map cleaning |
| `clean_rooms` | Start cleaning selected room segment IDs |
| `pause`, `stop`, `return_to_dock` | Control the current job |
| `locate` | Play the robot's locate sound |
| `set_fan_power` | Set a fan name or numeric code |
| `drive` | Experimental manual movement; model-dependent |

Cleaning commands return after dispatch, not after cleaning finishes. Poll
`get_status` to follow progress. Use its fan-speed mapping instead of assuming
codes are identical across models. Cleaning history may not identify every room
in older selective-clean jobs; don't infer exact room coverage from missing data.

## Security and operating limits

- This is a low-level control server, **not a safety-policy assistant**. It does
  not enforce user confirmation, quiet hours, battery thresholds, or schedules.
  Configure explicit approval for action tools in your MCP client. For read-only
  use, enforce a tool allowlist in a trusted client or gateway; a prompt alone
  is not an access-control boundary.
- Credentials grant control of your vacuum. Protect both the token file and
  client configuration. Gitignore is a convenience, not a secret scanner.
- Stdio opens no HTTP listener. Do not expose the server through an unauthenticated
  network bridge or give untrusted agents access to its process.
- The library uses Roborock account/cloud services and LAN connectivity where
  available. This is not guaranteed to operate fully offline.
- Rooms, history, device IDs and error output can reveal household information.
  An external model provider may receive tool results through your MCP client.
- Keep calls on one long-lived asyncio event loop. Embedded clients must await
  `close_connection()` on that loop before shutting it down.
- Experimental driving is not comprehensively bounded or validated by this
  server. Leave it disabled in your client unless you intend to test it.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use mocks and do not authenticate or move a robot. The extraction was also
checked by starting the server over stdio and listing tools without credentials.

## License

MIT. Unofficial project; not affiliated with Roborock.
