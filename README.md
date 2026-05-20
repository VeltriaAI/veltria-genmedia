# veltria-genmedia

> Multi-provider AI media generation — **one MCP server for image, video, and text** — proxied through any OpenAI-compatible gateway.

A thin Python MCP server that gives Claude Desktop, Claude Code, or any MCP-compatible client three tools: `gen_image`, `gen_video`, `gen_text`. It forwards calls to a gateway you control (LiteLLM, OpenRouter, Helicone, a Cloudflare AI Gateway, or your own proxy) so you can:

- Centralize **API key management** (one virtual key per user)
- Enforce **per-user budgets and rate limits** at the gateway
- **Revoke access** in seconds without touching anyone's machine
- Mix providers (Google Vertex · OpenAI · Anthropic · ElevenLabs · …) behind one endpoint

Originally built as the media-generation layer for the [Veltria Beings Protocol](https://github.com/VeltriaAI/beings-protocol). Generic enough to drop into any team workflow.

---

## Why a gateway-based MCP (vs. direct provider SDKs)

| | Direct provider SDKs in each user's machine | This (gateway-based) |
|---|---|---|
| Credentials per user | One per provider × every user | One virtual key per user |
| Budget enforcement | None | Hard caps at the gateway |
| Revoke access | Rotate keys across machines | Disable key in admin panel |
| Spend visibility | Aggregated in each provider's billing | Per-user, per-model, real-time |
| Multi-provider | New SDK + auth per provider | All providers behind one endpoint |
| Onboarding new user | Provision keys at every provider | Issue one virtual key |

---

## What's inside

| Tool exposed to your MCP client | Models (whatever your gateway routes to) | Reference media? |
|---|---|---|
| `gen_image` | Nano Banana, Imagen, DALL·E, FLUX, … | ✓ `reference_images` (list) — for edit / restyle / composition / style reference |
| `gen_video` | Veo, Sora, Runway, … | ✓ `reference_image` (single) — for image-to-video / first-frame seed |
| `gen_text` | Gemini Flash, Claude Haiku, GPT-4o-mini, … | — |

Models are configured at your **gateway**, not in this repo. Whatever names your gateway exposes (`gpt-image-1`, `nano-banana`, `veo-3`, …) are the names you pass via the `model` argument. The server is provider-agnostic.

### Reference media

Both image and video tools accept reference inputs. Each is either an absolute file path, an `http(s)://` URL, or a `data:` URI:

```
gen_image(
  prompt="Make this look like a Studio Ghibli scene",
  model="nano-banana",
  reference_images=["/Users/you/Desktop/photo.png"],
)

gen_video(
  prompt="Slow zoom in, golden-hour light",
  model="veo-3",
  reference_image="/Users/you/Desktop/logo.png",
)
```

When `reference_images` is set, `gen_image` routes via `/v1/chat/completions` with multimodal content blocks (the shape Gemini 2.5/3, GPT-4o, and Claude understand). Without references it uses `/v1/images/generations` as before. `gen_video` always uses `/v1/video/generations`; the reference image is passed as the first-frame seed.

---

## Quick install

**One-liner (recommended):**

```bash
curl -fsSL https://raw.githubusercontent.com/VeltriaAI/veltria-genmedia/main/scripts/install.sh | bash
```

The installer self-clones to `~/skills/veltria-genmedia` and re-executes from there. If you'd rather inspect first (good practice for any `curl | bash`), download then run:

```bash
curl -fsSL https://raw.githubusercontent.com/VeltriaAI/veltria-genmedia/main/scripts/install.sh -o install.sh
less install.sh           # review it
bash install.sh
```

**Manual clone:**

```bash
git clone https://github.com/VeltriaAI/veltria-genmedia.git ~/skills/veltria-genmedia
~/skills/veltria-genmedia/scripts/install.sh
```

**Custom install location:**

```bash
GENMEDIA_INSTALL_DIR=$HOME/tools/genmedia \
  bash <(curl -fsSL https://raw.githubusercontent.com/VeltriaAI/veltria-genmedia/main/scripts/install.sh)
```

The wizard asks for:

1. **Gateway base URL** — e.g. `https://your-gateway.example.com` (any OpenAI-compatible endpoint)
2. **API key** — your virtual key issued by the gateway admin
3. **Which client to wire** — Claude Desktop, Claude Code, or both
4. **Output directories** — where generated images / videos should land
5. **Optional brand preset** — a free-form string injected into every image prompt (e.g. *"warm cream palette, hand-drawn aesthetic"*); leave blank for raw passthrough

Then it installs Python deps, writes a config file at `~/.config/veltria-genmedia/gateway.env` (`chmod 600`), wires the chosen client(s), and verifies end-to-end.

Full step-by-step in [`INSTALL.md`](INSTALL.md).

---

## Architecture (one paragraph)

You ask Claude to generate something. Claude calls this local MCP server. The server reads the user's config, signs the request with their virtual key, and POSTs to the configured gateway's OpenAI-compatible endpoints (`/v1/images/generations`, `/v1/chat/completions`, etc.). The result comes back; binary outputs are saved to the user's chosen local folder. The file path is returned to Claude.

**The user never touches the upstream provider's SDK, credentials, or billing.** All they need is the gateway URL and a per-user API key.

Full design notes in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Repo layout

```
.
├── README.md                — you are here
├── INSTALL.md               — wizard walkthrough
├── ARCHITECTURE.md          — design + extension notes
├── LICENSE                  — Apache 2.0
├── config/
│   ├── gateway.env.example
│   ├── claude_desktop_config.example.json
│   └── claude_code_settings.example.json
├── mcp/
│   ├── server.py            — the MCP server (~250 lines, single file)
│   ├── pyproject.toml
│   └── README.md
├── scripts/
│   ├── install.sh           — wizard entry point
│   └── verify.sh            — gateway health + auth check
└── docs/
    ├── PROVIDERS.md         — patterns for common gateways / models
    └── TROUBLESHOOTING.md
```

---

## Pairs well with

- **[LiteLLM](https://github.com/BerriAI/litellm)** — the most common OpenAI-compatible gateway, supports 100+ providers with budget + RPM enforcement built-in
- **[OpenRouter](https://openrouter.ai/)** — managed gateway across many providers
- **[Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/)** — when you want CF's caching + observability
- **[Helicone](https://www.helicone.ai/)** — when you want a hosted observability layer
- Your own LLM proxy

Any endpoint that speaks OpenAI's `/v1/*` JSON schema works.

---

## Contributing

Issues and PRs welcome. Code style: black + isort for Python, shellcheck-clean for bash.

To add a new tool (e.g., audio generation), edit `mcp/server.py` — there's a one-paragraph guide in [`mcp/README.md`](mcp/README.md#adding-a-new-tool).

---

## License

Apache 2.0. See [`LICENSE`](LICENSE).

---

*Maintained by [Veltria AI](https://github.com/VeltriaAI).*
