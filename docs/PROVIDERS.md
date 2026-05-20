# Providers & gateway patterns

This MCP server is **provider-agnostic** — it speaks OpenAI-compatible HTTP. The actual model catalog lives at *your gateway*, not in this repo. Below are common gateway setups and which models they expose.

## Pattern A — [LiteLLM](https://github.com/BerriAI/litellm) (self-hosted)

The most flexible option. Self-host LiteLLM in front of Vertex AI / OpenAI / Anthropic / Bedrock / 100+ providers. Built-in support for:

- Per-user virtual keys
- Per-model budgets + RPM limits
- Spend tracking + admin UI
- Provider failover

Typical model exposures:

| Gateway model name | Routes to |
|---|---|
| `nano-banana` | Vertex AI · `gemini-2.5-flash-image` |
| `imagen-3` | Vertex AI · `imagen-3.0-generate-002` |
| `veo-3`, `veo-2` | Vertex AI · Veo |
| `gpt-image-1` | OpenAI image gen |
| `gemini-flash` | Vertex AI · `gemini-2.0-flash` |
| `claude-haiku`, `claude-sonnet` | Anthropic (direct) or Vertex AI · Claude |
| `gpt-4o-mini` | OpenAI |

You define the mapping in `litellm-config.yaml`. Whatever name you pick is what users pass as the `model` argument to this MCP server.

## Pattern B — [OpenRouter](https://openrouter.ai/)

Managed gateway with one credential, model marketplace across providers.

Set in `gateway.env`:

```env
GATEWAY_BASE_URL=https://openrouter.ai/api
GATEWAY_API_KEY=sk-or-v1-...
AUTH_HEADER=authorization
```

Model names match OpenRouter's catalog (`google/gemini-flash-1.5`, `anthropic/claude-3-haiku`, `openai/dall-e-3`, …).

## Pattern C — [Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/)

Use when you want CF's caching, observability, rate limiting in front of your real providers. CF AI Gateway speaks OpenAI-compatible mode.

```env
GATEWAY_BASE_URL=https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/compat
GATEWAY_API_KEY=<your-token>
AUTH_HEADER=authorization
```

## Pattern D — your own proxy

Anything that accepts POST to `/v1/images/generations`, `/v1/video/generations`, `/v1/chat/completions` with a JSON body and the standard OpenAI request schema works. Roll your own with FastAPI + httpx in ~50 lines if needed.

---

## Selection heuristics (typical defaults)

| Use case | Suggested gateway model |
|---|---|
| Fast everyday image gen | Whatever your gateway routes to Gemini 2.5/3 Image (often called `nano-banana`) or FLUX schnell |
| Premium hero / cover image | Imagen 3, DALL·E 3, FLUX Pro |
| 5–10 sec product reel | Veo 3 (highest quality), Sora 2 (different style), Runway Gen-3 |
| Image-to-video animation | Veo 3.1 image-to-video, Runway Gen-3 |
| Captions / alt-text | Gemini Flash, Claude Haiku, GPT-4o-mini — pennies per call |
| Long-form drafting | Claude Sonnet or GPT-4o — costs more, quality follows |

---

## Brand preset injection

If you set `BRAND_PRESET=...` in your `gateway.env`, that string is prepended to every `gen_image` prompt. Useful for keeping team output on-brand without typing the cue each time.

Example brand presets you might use:

```env
# Editorial / newsroom
BRAND_PRESET=Editorial photography style, natural lighting, no stock-photo aesthetic, muted color palette.

# Studio / animated
BRAND_PRESET=Hand-drawn animation style, warm palette, playful, NOT photorealistic.

# Corporate clean
BRAND_PRESET=Brand: Acme Corp. Palette — navy #002040 and gold #FFC107. Clean editorial. No generic SaaS gradients.
```

Per-call opt-out: pass `raw=true` to the tool (or tell Claude *"raw output, no brand"*).

---

## Cost-aware defaults

The gateway is the right layer for cost guardrails — `litellm` and similar all support:

- Per-key budget caps (hard stop or alert at threshold)
- Per-key RPM (rate limits)
- Per-model allowlists (block expensive models for cheap tiers)

Configure those at the gateway. This MCP server doesn't second-guess your gateway's enforcement.

If you want a client-side soft warning (e.g., *"this call will cost ~$3, confirm?"*), wrap calls in a Claude skill that asks for confirmation before invoking `gen_video` — not built-in here.
