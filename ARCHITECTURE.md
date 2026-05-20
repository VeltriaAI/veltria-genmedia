# Architecture

## The picture

```
┌────────────────────────────────────────────┐
│ User                                       │
│ Claude Desktop  or  Claude Code            │
│ "Generate a banner for our launch…"         │
└──────────────────┬─────────────────────────┘
                   │ MCP over stdio
                   ▼
┌────────────────────────────────────────────┐
│ Local MCP server (mcp/server.py)           │
│ • Reads ~/.config/veltria-genmedia/        │
│   gateway.env on every call (no cache)     │
│ • Exposes gen_image / gen_video / gen_text │
└──────────────────┬─────────────────────────┘
                   │ HTTPS · OpenAI-compatible
                   │ Authorization or x-api-key
                   ▼
┌────────────────────────────────────────────┐
│ Your gateway                               │
│ (LiteLLM · OpenRouter · Helicone · CF AI · │
│  your own proxy)                           │
│                                            │
│ • Per-user virtual keys                    │
│ • Per-user budgets + rate limits           │
│ • Spend tracking                           │
│ • Provider routing                         │
└──────────────────┬─────────────────────────┘
                   │ provider SDKs
                   ▼
┌────────────────────────────────────────────┐
│ Image / video / text providers             │
│ Vertex AI · OpenAI · Anthropic · etc.      │
└────────────────────────────────────────────┘
```

## Why this shape

### vs. provider SDKs directly on user machines

Putting provider SDKs (Google Cloud, OpenAI, etc.) on every user's machine is the path of least resistance — but it makes the org pay in three places:

- **Onboarding** — every new user gets credentials at every provider (multi-step IAM)
- **Budget control** — no hard caps, runaway scripts can drain a card
- **Revocation** — pulling someone's access requires touching every provider, and any credential file they downloaded stays on their machine until you ask them to delete it

Putting a gateway between users and providers fixes all three with a single API key per user.

### Why MCP + HTTP gateway (instead of just `curl`)

OpenAI-compatible HTTP endpoints can be called by anything. So why a Python MCP server?

- **Structured tool calls in Claude** — Claude sees real named tools (`gen_image`, `gen_video`) instead of "run this curl command"
- **Local file handling** — the server downloads binary results and saves to the user's `~/Pictures/...` automatically; harder to do cleanly from a shell skill
- **One install, two clients** — Claude Desktop and Claude Code both speak MCP; same `mcp/server.py` works for both
- **Future-proof** — when your gateway adds new endpoints (audio, embeddings), add new tools to `server.py` without changing user setup

### Why Python for the MCP server

- Most LLM gateway tooling is Python (LiteLLM, LangChain, etc.) — staying in-language for easy extension
- Official `mcp` SDK is mature in Python
- ~250 lines total, single file — small enough to audit in 5 minutes
- No virtualenv needed; `pip install --user` keeps install simple for non-technical teammates

## Auth model

| Hop | How auth flows |
|---|---|
| **Claude → Local MCP server** | None — stdio loopback, no network |
| **Local MCP server → gateway** | Per-user virtual key from `gateway.env`; default header `x-api-key`, override to `Authorization: Bearer …` via `AUTH_HEADER=authorization` |
| **Gateway → upstream providers** | Gateway's responsibility (out of scope for this repo) |

The virtual key lives at `~/.config/veltria-genmedia/gateway.env` with `chmod 600`. The MCP server reads it on **every call** — no caching — so rotation is immediate: overwrite the file, no restart needed.

## Brand-preset injection

If you set `BRAND_PRESET=...` in `gateway.env`, that string is prepended to every `gen_image` prompt (skippable per-call by passing `raw=true`). This is a convenience for teams who want consistent brand cues without typing them every time. **No defaults are shipped** — you decide what gets injected (or leave blank).

Example:

```env
BRAND_PRESET=Brand: Acme Corp. Palette — navy #002040 and gold #FFC107, clean editorial aesthetic, no generic SaaS gradients.
```

## What's not here

- **Audio generation** — easy to add; see [`mcp/README.md`](mcp/README.md#adding-a-new-tool). Not shipped by default to keep the surface small.
- **Spend dashboard** — that lives at the gateway. Most gateways have admin UIs; this repo doesn't try to replicate them.
- **Auto-upload to S3 / OneDrive / Dropbox** — outputs are local files; pipe wherever you want with shell tooling.
- **Caching** — pass-through. Cache at the gateway (LiteLLM, CF AI Gateway, etc. all support response caching).

## Extension points

| You want… | Edit |
|---|---|
| New tool (audio, embeddings) | `mcp/server.py` — add to `list_tools()` + `call_tool()` |
| Different config path | `mcp/server.py` line ~34 (`CONFIG_PATH`) |
| Different auth header | `gateway.env` → `AUTH_HEADER=authorization` |
| Brand presets | `gateway.env` → `BRAND_PRESET=...` |
| Output filename convention | `mcp/server.py` `_slug()` and `_stamp()` |
| Retry / backoff | currently `MAX_RETRIES=2`; extend in `call_image` / `call_video` |

## Maintenance

This repo intentionally does very little — it's a thin gateway shim. Most changes you'll make will be to the **gateway config** (adding/removing models, adjusting budgets, routing rules), not this code.

When this repo does change (typically: new tool surface, MCP SDK upgrade), users `git pull && ./scripts/install.sh --headless` to refresh. Their config stays put.
