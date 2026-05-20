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


# ─────────────────────────────────────────────────────────────────────────────
# Gateway calls
# ─────────────────────────────────────────────────────────────────────────────


async def call_image(cfg: dict[str, str], *, prompt: str, model: str, n: int, size: str | None, raw: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": _brand_prefix(cfg, raw) + prompt,
        "n": n,
        "response_format": "b64_json",
    }
    if size:
        payload["size"] = size
    timeout = int(cfg.get("IMAGE_TIMEOUT", "120"))
    url = cfg["GATEWAY_BASE_URL"].rstrip("/") + "/v1/images/generations"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers(cfg))
    resp.raise_for_status()
    return resp.json()


async def call_video(cfg: dict[str, str], *, prompt: str, model: str, duration_sec: int, aspect_ratio: str | None, raw: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": _brand_prefix(cfg, raw) + prompt,
        "duration_seconds": duration_sec,
    }
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    timeout = int(cfg.get("VIDEO_TIMEOUT", "600"))
    url = cfg["GATEWAY_BASE_URL"].rstrip("/") + "/v1/video/generations"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=_headers(cfg))
    resp.raise_for_status()
    return resp.json()


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
    paths: list[str] = []
    outdir = _outdir(cfg, "video", output_dir)
    slug = _slug(prompt)
    stamp = _stamp()
    for i, item in enumerate(result.get("data", [])):
        suffix = "" if i == 0 else f"-{i}"
        fname = f"{stamp}-{slug}{suffix}.mp4"
        out = outdir / fname
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
                "Generate one or more images via your configured OpenAI-compatible "
                "gateway. Model names must match what your gateway exposes (e.g. "
                "'nano-banana', 'imagen-3', 'gpt-image-1', 'flux-pro'). "
                "BRAND_PRESET (if configured) is auto-prepended unless raw=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to generate"},
                    "model": {"type": "string", "description": "Model name as configured on your gateway"},
                    "n": {"type": "integer", "description": "Number of images", "default": 1, "minimum": 1, "maximum": 4},
                    "size": {"type": "string", "description": "e.g. 1024x1024, 1792x1024 (model-dependent)"},
                    "output_dir": {"type": "string", "description": "Override default save directory"},
                    "raw": {"type": "boolean", "description": "Skip BRAND_PRESET injection", "default": False},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="gen_video",
            description=(
                "Generate a short video clip via your gateway. Model names must "
                "match what your gateway exposes (e.g. 'veo-3', 'veo-2', "
                "'sora-2', 'runway-gen3'). Videos are expensive — use thoughtfully."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string", "description": "Model name as configured on your gateway"},
                    "duration_sec": {"type": "integer", "description": "Clip length", "default": 5, "minimum": 1, "maximum": 30},
                    "aspect_ratio": {"type": "string", "description": "e.g. 16:9, 9:16, 1:1"},
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
        try:
            result = await call_image(
                cfg,
                prompt=arguments["prompt"],
                model=arguments.get("model", cfg.get("DEFAULT_IMAGE_MODEL", "")),
                n=int(arguments.get("n", 1)),
                size=arguments.get("size"),
                raw=bool(arguments.get("raw", False)),
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
