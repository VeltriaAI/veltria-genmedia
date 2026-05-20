#!/usr/bin/env python3
"""
veltria-genmedia — MCP server

Thin proxy from Claude (Desktop / Code) → an OpenAI-compatible gateway
(LiteLLM, OpenRouter, Helicone, Cloudflare AI Gateway, your own proxy).

Exposes three tools: gen_image, gen_video, gen_text.

Reads config from ~/.config/veltria-genmedia/gateway.env on every call (no
caching), so virtual-key rotation is immediate — overwrite the file, no
restart needed.

Tested with Python 3.11+.
License: Apache 2.0
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ─────────────────────────────────────────────────────────────────────────────
# Config loader — reads ~/.config/veltria-genmedia/gateway.env on every call
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".config" / "veltria-genmedia" / "gateway.env"


def _expand(value: str) -> str:
    """Expand $HOME / ~ / $VAR references."""
    return os.path.expandvars(os.path.expanduser(value))


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Config not found at {CONFIG_PATH}.\n"
            "Run scripts/install.sh from the repo root to set it up."
        )
    out: dict[str, str] = {}
    for raw in CONFIG_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = _expand(v.strip())
    if "GATEWAY_BASE_URL" not in out or "GATEWAY_API_KEY" not in out:
        raise RuntimeError(
            f"Config at {CONFIG_PATH} is missing GATEWAY_BASE_URL or GATEWAY_API_KEY.\n"
            "Re-run scripts/install.sh."
        )
    return out


def _outdir(cfg: dict[str, str], category: str, override: str | None) -> Path:
    if override:
        path = Path(_expand(override))
    elif category == "image":
        path = Path(cfg.get("DEFAULT_IMAGE_OUTPUT_DIR", "~/Pictures/genmedia"))
    else:
        path = Path(cfg.get("DEFAULT_VIDEO_OUTPUT_DIR", "~/Movies/genmedia"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(prompt: str, n: int = 6) -> str:
    """Trim a prompt to a short filename-safe slug."""
    safe = "".join(c if c.isalnum() else "-" for c in prompt[:60]).strip("-")
    return safe[: 8 * n] or "image"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _headers(cfg: dict[str, str]) -> dict[str, str]:
    header_name = cfg.get("AUTH_HEADER", "x-api-key").lower()
    key = cfg["GATEWAY_API_KEY"]
    if header_name == "authorization":
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return {header_name: key, "Content-Type": "application/json"}


def _brand_prefix(cfg: dict[str, str], raw: bool) -> str:
    """Inject BRAND_PRESET into image prompts unless raw=True. No defaults shipped."""
    if raw:
        return ""
    preset = cfg.get("BRAND_PRESET", "").strip()
    return (preset + " ") if preset else ""


def _load_reference(path_or_url: str) -> str:
    """Resolve a reference image/video input to a value the gateway can ingest.

    Accepts:
      - http(s):// URLs → returned as-is (gateway fetches them)
      - data:... URIs   → returned as-is
      - local file paths → read bytes, base64-encode, return a data: URI

    Returns a string usable as `image_url.url` in chat/completions multimodal
    content, or as the value of an `image` field in video payloads.
    """
    s = path_or_url.strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    p = Path(_expand(s))
    if not p.is_file():
        raise RuntimeError(f"Reference not found at {p} (give an absolute path, http(s):// URL, or data: URI)")
    mime, _ = mimetypes.guess_type(p.name)
    if not mime:
        # Reasonable defaults by suffix; fall back to octet-stream.
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif",
            ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        }.get(p.suffix.lower(), "application/octet-stream")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ─────────────────────────────────────────────────────────────────────────────
# Gateway calls
# ─────────────────────────────────────────────────────────────────────────────


async def call_image(
    cfg: dict[str, str],
    *,
    prompt: str,
    model: str,
    n: int,
    size: str | None,
    raw: bool,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """Two code paths:

      - WITHOUT references → POST /v1/images/generations (OpenAI text-to-image)
      - WITH references    → POST /v1/chat/completions  (multimodal: text + image
        content blocks). This is the shape Gemini 2.5/3 Flash Image, Claude,
        GPT-4o, etc. understand for "edit / compose / use this as reference".

    Returns a dict in a unified shape: {"data": [{"b64_json": "..."} or {"url": "..."}]}
    so save_image_result() doesn't care which endpoint we used.
    """
    full_prompt = _brand_prefix(cfg, raw) + prompt
    timeout = int(cfg.get("IMAGE_TIMEOUT", "120"))
    base = cfg["GATEWAY_BASE_URL"].rstrip("/")

    if reference_images:
        # Multimodal chat-completions path. n is intentionally ignored — chat
        # APIs return one assistant message per call; loop client-side if you
        # want more variants.
        content: list[dict[str, Any]] = [{"type": "text", "text": full_prompt}]
        for ref in reference_images:
            content.append({"type": "image_url", "image_url": {"url": _load_reference(ref)}})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            # Some gateways honour these; harmless when ignored.
            "modalities": ["image", "text"],
        }
        url = base + "/v1/chat/completions"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=_headers(cfg))
        resp.raise_for_status()
        return _normalize_chat_image_response(resp.json())

    # Text-to-image path (unchanged).
    payload = {
        "model": model,
        "prompt": full_prompt,
        "n": n,
        "response_format": "b64_json",
    }
    if size:
        payload["size"] = size
    url = base + "/v1/images/generations"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers(cfg))
    resp.raise_for_status()
    return resp.json()


def _normalize_chat_image_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Pull image bytes/URLs out of a chat-completions response and reshape to
    the /v1/images/generations envelope: {"data": [{"b64_json": "..."}, ...]}.

    Gateways differ in where they put the image:
      - choices[0].message.images = [{"image_url": {"url": "data:image/png;base64,..."}}]
      - choices[0].message.content = [{"type": "image", "source": {...b64...}}]   (Anthropic-style)
      - choices[0].message.content = [{"type": "image_url", "image_url": {"url": "data:..."}}]
      - choices[0].message.content = "<base64 blob>"  (some Vertex paths)
    Handle each, skip text-only parts.
    """
    out: list[dict[str, str]] = []
    for choice in raw.get("choices", []) or []:
        msg = choice.get("message") or {}
        # Modern LiteLLM/Gemini path
        for item in msg.get("images") or []:
            url = (item.get("image_url") or {}).get("url") or item.get("url")
            if isinstance(url, str):
                _push_image_url(out, url)
        # Multimodal content blocks
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "image_url":
                    url = (part.get("image_url") or {}).get("url")
                    if isinstance(url, str):
                        _push_image_url(out, url)
                elif ptype in ("image", "output_image"):
                    src = part.get("source") or {}
                    if src.get("type") == "base64" and src.get("data"):
                        out.append({"b64_json": src["data"]})
                    elif src.get("type") == "url" and src.get("url"):
                        _push_image_url(out, src["url"])
                    elif part.get("b64_json"):
                        out.append({"b64_json": part["b64_json"]})
    return {"data": out, "_raw": raw}


def _push_image_url(out: list[dict[str, str]], url: str) -> None:
    """Append either a {b64_json} (if data: URI) or a {url} entry."""
    if url.startswith("data:") and "base64," in url:
        out.append({"b64_json": url.split("base64,", 1)[1]})
    else:
        out.append({"url": url})


# Veo single-shot generation tops out at 8 seconds. For longer clips we chain
# via /v1/videos/extensions, each extension adding up to 8 more sec onto the
# previous video. So a 24-sec ask = 1 base (8) + 2 extensions (8 each).
VEO_CHUNK_MAX = 8
VEO_CHUNK_MIN = 5  # Veo rejects < 5 sec per chunk


def _aspect_to_size(aspect_ratio: str | None) -> str | None:
    """Map common aspect ratios to a 'WxH' pixel size string. Pass-through if
    the input already looks like 'WxH'."""
    if not aspect_ratio:
        return None
    if "x" in aspect_ratio.lower() and aspect_ratio.lower().replace("x", "").isdigit():
        return aspect_ratio
    return {
        "16:9": "1280x720",
        "9:16": "720x1280",
        "1:1":  "1024x1024",
        "4:3":  "1024x768",
        "3:4":  "768x1024",
    }.get(aspect_ratio.strip(), None)


async def _poll_video_job(
    client: httpx.AsyncClient,
    *,
    base: str,
    headers: dict[str, str],
    job_id: str,
    deadline: float,
) -> dict[str, Any]:
    """Block until the gateway reports completed/failed. Veo jobs typically take
    30-90s for an 8-sec clip; back off from 5s to 15s so we don't hammer."""
    backoff = 5
    while time.time() < deadline:
        r = await client.get(f"{base}/v1/videos/{job_id}", headers=headers)
        r.raise_for_status()
        d = r.json()
        status = d.get("status")
        if status == "completed":
            return d
        if status == "failed":
            err = d.get("error") or {}
            raise RuntimeError(
                f"Video job failed: {err.get('message') or err}"
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff + 2, 15)
    raise RuntimeError(f"Video job {job_id} timed out (raise VIDEO_TIMEOUT in gateway.env)")


async def call_video(
    cfg: dict[str, str],
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    aspect_ratio: str | None,
    raw: bool,
    reference_image: str | None = None,
) -> dict[str, Any]:
    """Async Veo flow:

      1. POST /v1/videos              → returns a job (status: processing)
      2. GET  /v1/videos/{id}         → poll until status: completed/failed
      3. If duration > 8s, chain:
         POST /v1/videos/extensions   → new job extending the prior one
         (poll again, repeat until target duration reached)
      4. GET  /v1/videos/{id}/content → MP4 bytes

    Returns {'_video_bytes': bytes, '_video_id': str, '_seconds': int}.
    The bytes are saved to disk by save_video_result().
    """
    full_prompt = _brand_prefix(cfg, raw) + prompt
    headers = _headers(cfg)
    base = cfg["GATEWAY_BASE_URL"].rstrip("/")
    job_timeout = int(cfg.get("VIDEO_TIMEOUT", "600"))
    deadline = time.time() + job_timeout

    # Clamp: Veo refuses <5s per chunk; cap the overall ask to keep runaway
    # extension chains from blowing budget by accident.
    total = max(VEO_CHUNK_MIN, min(int(duration_sec), 60))

    # Plan: first chunk up to 8s, then 8s extensions until we hit the target.
    first_chunk = min(total, VEO_CHUNK_MAX)
    remaining = total - first_chunk

    size = _aspect_to_size(aspect_ratio)

    # 1. Initial generation
    init_payload: dict[str, Any] = {
        "model": model,
        "prompt": full_prompt,
        "seconds": first_chunk,
    }
    if size:
        init_payload["size"] = size
    if reference_image:
        # OpenAI Sora-style field name; LiteLLM maps it to Vertex's image-to-
        # video input for Veo.
        init_payload["input_reference"] = _load_reference(reference_image)

    async with httpx.AsyncClient(timeout=job_timeout) as client:
        resp = await client.post(f"{base}/v1/videos", json=init_payload, headers=headers)
        resp.raise_for_status()
        job = resp.json()
        video_id = job["id"]
        await _poll_video_job(client, base=base, headers=headers, job_id=video_id, deadline=deadline)

        # 2. Extensions (only if user asked for > 8s)
        while remaining > 0:
            chunk = min(VEO_CHUNK_MAX, max(VEO_CHUNK_MIN, remaining))
            ext_payload = {
                "model": model,
                "prompt": full_prompt,
                "seconds": chunk,
                "video": {"id": video_id},
            }
            er = await client.post(f"{base}/v1/videos/extensions", json=ext_payload, headers=headers)
            er.raise_for_status()
            video_id = er.json()["id"]
            await _poll_video_job(client, base=base, headers=headers, job_id=video_id, deadline=deadline)
            remaining -= chunk

        # 3. Fetch the bytes
        cr = await client.get(f"{base}/v1/videos/{video_id}/content", headers=headers)
        cr.raise_for_status()

    return {
        "_video_bytes": cr.content,
        "_video_id": video_id,
        "_seconds": total,
    }


async def call_chat(cfg: dict[str, str], *, prompt: str, model: str, system: str | None) -> str:
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": msgs, "max_tokens": 4096}
    timeout = 60
    url = cfg["GATEWAY_BASE_URL"].rstrip("/") + "/v1/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers(cfg))
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Result handlers — save to disk
# ─────────────────────────────────────────────────────────────────────────────


def save_image_result(cfg: dict[str, str], *, result: dict[str, Any], prompt: str, output_dir: str | None) -> list[str]:
    """Save returned image bytes (b64 or url) to local disk. Returns list of paths."""
    paths: list[str] = []
    outdir = _outdir(cfg, "image", output_dir)
    slug = _slug(prompt)
    stamp = _stamp()
    for i, item in enumerate(result.get("data", [])):
        suffix = "" if i == 0 else f"-{i}"
        fname = f"{stamp}-{slug}{suffix}.png"
        out = outdir / fname
        if "b64_json" in item and item["b64_json"]:
            out.write_bytes(base64.b64decode(item["b64_json"]))
        elif "url" in item and item["url"]:
            with httpx.Client(timeout=60) as c:
                r = c.get(item["url"])
                r.raise_for_status()
                out.write_bytes(r.content)
        else:
            continue
        paths.append(str(out))
    return paths


def save_video_result(cfg: dict[str, str], *, result: dict[str, Any], prompt: str, output_dir: str | None) -> list[str]:
    """call_video returns one of two shapes:

      - {'_video_bytes': <bytes>, ...}             (new — async Veo flow, content already fetched)
      - {'data': [{'url': ...}, ...]}              (legacy — kept for gateways that
                                                    return a direct URL)
    """
    paths: list[str] = []
    outdir = _outdir(cfg, "video", output_dir)
    slug = _slug(prompt)
    stamp = _stamp()

    if "_video_bytes" in result:
        out = outdir / f"{stamp}-{slug}.mp4"
        out.write_bytes(result["_video_bytes"])
        paths.append(str(out))
        return paths

    for i, item in enumerate(result.get("data", [])):
        suffix = "" if i == 0 else f"-{i}"
        out = outdir / f"{stamp}-{slug}{suffix}.mp4"
        url = item.get("url") or item.get("video_url")
        if not url:
            continue
        with httpx.Client(timeout=300) as c:
            r = c.get(url)
            r.raise_for_status()
            out.write_bytes(r.content)
        paths.append(str(out))
    return paths


# ─────────────────────────────────────────────────────────────────────────────
# MCP server
# ─────────────────────────────────────────────────────────────────────────────

server = Server("veltria-genmedia")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="gen_image",
            description=(
                "Generate or EDIT images via your configured OpenAI-compatible "
                "gateway. Model names must match what your gateway exposes (e.g. "
                "'nano-banana', 'imagen-3', 'gpt-image-1', 'flux-pro'). "
                "BRAND_PRESET (if configured) is auto-prepended unless raw=true. "
                "Pass `reference_images` (file paths or URLs) to do image-to-image: "
                "edit, restyle, compose, or use them as style/subject references. "
                "Works best with multimodal models like nano-banana."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to generate, or what to do with the reference(s)"},
                    "model": {"type": "string", "description": "Model name as configured on your gateway"},
                    "n": {"type": "integer", "description": "Number of images (ignored when reference_images is used)", "default": 1, "minimum": 1, "maximum": 4},
                    "size": {"type": "string", "description": "e.g. 1024x1024, 1792x1024 (model-dependent)"},
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of reference inputs. Each entry is either an absolute file path on disk (e.g. /Users/.../photo.png), an http(s):// URL, or a data: URI. When provided, routes via chat/completions for multimodal generation.",
                    },
                    "output_dir": {"type": "string", "description": "Override default save directory"},
                    "raw": {"type": "boolean", "description": "Skip BRAND_PRESET injection", "default": False},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="gen_video",
            description=(
                "Generate a video clip via your gateway. Async flow under the "
                "hood: POSTs the job, polls until ready, fetches the MP4. "
                "Model names must match what your gateway exposes ('veo-3', "
                "'veo-2', 'sora-2', 'runway-gen3'). "
                "Veo caps each generation at 8 seconds; longer durations are "
                "automatically chained as extensions (8-sec increments), so "
                "duration_sec=24 = 1 base + 2 extensions. Each chained chunk "
                "is a separate Veo call that costs ~$0.10–0.35/sec, so a "
                "30-sec clip can easily be $5–10 — use thoughtfully. "
                "Pass `reference_image` to animate a still photo "
                "(image-to-video / first-frame conditioning). "
                "Each generation takes 30–90 seconds; multi-chunk clips "
                "multiply that. Patient mode."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string", "description": "Model name as configured on your gateway"},
                    "duration_sec": {
                        "type": "integer",
                        "description": "Target clip length in seconds. Clamped to 5–60. Veo's per-call max is 8s; longer values are chained via /v1/videos/extensions (8-sec chunks).",
                        "default": 5,
                        "minimum": 5,
                        "maximum": 60,
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Convenience aspect: '16:9', '9:16', '1:1', '4:3', '3:4' — auto-converted to size. You can also pass a raw 'WxH' (e.g. '1280x720') and it's used as-is.",
                    },
                    "reference_image": {
                        "type": "string",
                        "description": "Optional first-frame seed. File path on disk, http(s):// URL, or data: URI. Used for image-to-video animation.",
                    },
                    "output_dir": {"type": "string"},
                    "raw": {"type": "boolean", "default": False},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="gen_text",
            description=(
                "Draft text (captions, social posts, alt-text, summaries) via your "
                "gateway's chat/completions endpoint. Model names must match what "
                "your gateway exposes (e.g. 'gemini-flash', 'claude-haiku', 'gpt-4o-mini')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string", "description": "Model name as configured on your gateway"},
                    "system": {"type": "string", "description": "Optional system instruction"},
                },
                "required": ["prompt"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        cfg = load_config()
    except RuntimeError as e:
        return [TextContent(type="text", text=f"❌ {e}")]

    started = time.time()

    if name == "gen_image":
        refs = arguments.get("reference_images")
        if refs is not None and not isinstance(refs, list):
            return [TextContent(type="text", text="❌ reference_images must be an array of file paths or URLs")]
        try:
            result = await call_image(
                cfg,
                prompt=arguments["prompt"],
                model=arguments.get("model", cfg.get("DEFAULT_IMAGE_MODEL", "")),
                n=int(arguments.get("n", 1)),
                size=arguments.get("size"),
                raw=bool(arguments.get("raw", False)),
                reference_images=refs,
            )
        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"❌ Gateway error {e.response.status_code}: {e.response.text[:500]}")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ {type(e).__name__}: {e}")]
        paths = save_image_result(cfg, result=result, prompt=arguments["prompt"], output_dir=arguments.get("output_dir"))
        if not paths:
            return [TextContent(type="text", text="⚠️ Gateway returned no image data. Full response:\n" + str(result)[:1500])]
        dur = time.time() - started
        return [TextContent(type="text", text=f"✅ Generated {len(paths)} image(s) in {dur:.1f}s:\n" + "\n".join(paths))]

    if name == "gen_video":
        try:
            result = await call_video(
                cfg,
                prompt=arguments["prompt"],
                model=arguments.get("model", cfg.get("DEFAULT_VIDEO_MODEL", "")),
                duration_sec=int(arguments.get("duration_sec", 5)),
                aspect_ratio=arguments.get("aspect_ratio"),
                raw=bool(arguments.get("raw", False)),
                reference_image=arguments.get("reference_image"),
            )
        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"❌ Gateway error {e.response.status_code}: {e.response.text[:500]}")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ {type(e).__name__}: {e}")]
        paths = save_video_result(cfg, result=result, prompt=arguments["prompt"], output_dir=arguments.get("output_dir"))
        if not paths:
            return [TextContent(type="text", text="⚠️ Gateway returned no video data. Full response:\n" + str(result)[:1500])]
        dur = time.time() - started
        return [TextContent(type="text", text=f"✅ Generated {len(paths)} video(s) in {dur:.1f}s:\n" + "\n".join(paths))]

    if name == "gen_text":
        try:
            text = await call_chat(
                cfg,
                prompt=arguments["prompt"],
                model=arguments.get("model", cfg.get("DEFAULT_TEXT_MODEL", "")),
                system=arguments.get("system"),
            )
        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"❌ Gateway error {e.response.status_code}: {e.response.text[:500]}")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ {type(e).__name__}: {e}")]
        return [TextContent(type="text", text=text)]

    return [TextContent(type="text", text=f"❌ Unknown tool: {name}")]


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
