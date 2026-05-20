# MCP Server

Thin Python MCP server that proxies Claude (Desktop / Code) → any OpenAI-compatible gateway.

## Files

| File | Purpose |
|---|---|
| `server.py` | The MCP server. Exposes `gen_image`, `gen_video`, `gen_text` tools. |
| `pyproject.toml` | Python deps + entry point |

## How it's invoked

MCP clients (Claude Desktop, Claude Code, …) spawn `python3 mcp/server.py` over stdio. The install wizard wires this up automatically.

Per-call flow:

1. Claude sends an MCP `tools/call` for one of: `gen_image` / `gen_video` / `gen_text`
2. `server.py` reads `~/.config/veltria-genmedia/gateway.env` fresh (no caching)
3. Constructs an OpenAI-compatible JSON request
4. POSTs to `${GATEWAY_BASE_URL}/v1/*` with the configured auth header + key
5. Saves any returned image/video bytes to the user's configured output directory
6. Returns the local file path back to Claude

## Dev / debugging

Run the server standalone to see startup errors:

```bash
python3 ~/skills/veltria-genmedia/mcp/server.py < /dev/null
```

It'll print initialization output to stderr. The server idles on stdin waiting for MCP frames — `Ctrl+C` to exit.

To smoke-test the gateway end-to-end without going through MCP:

```bash
~/skills/veltria-genmedia/scripts/verify.sh --image
```

## Adding a new tool

1. Add tool name + schema to the `list_tools()` function in `server.py`
2. Add a handler branch in `call_tool()`
3. Add the actual gateway call function (e.g., `call_audio()`)
4. Add a result-saver if it returns binary data
5. Restart the MCP client (Cmd+Q + reopen for Claude Desktop)

Example: adding audio generation against an OpenAI-compatible `/v1/audio/speech` endpoint takes ~30 lines following the existing `call_image` / `save_image_result` pattern.

## Adding a new model

No code change needed. Models are configured at the **gateway**, not in this code. Users just pass the model name (whatever your gateway exposes) as the `model` argument when calling `gen_image` / `gen_video` / `gen_text`.

To set the default for a user, edit their `gateway.env`:

```env
DEFAULT_IMAGE_MODEL=your-favorite-image-model
DEFAULT_VIDEO_MODEL=your-favorite-video-model
DEFAULT_TEXT_MODEL=your-favorite-text-model
```

## Dependencies

- `httpx >= 0.27` — async HTTP client
- `mcp >= 1.2.0` — official MCP Python SDK
- Python 3.11+

Installed user-level via `pip install --user httpx mcp` during install. No virtualenv — keeps install simple for non-technical teammates.

## License

Apache 2.0.
