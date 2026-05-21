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
import shutil
import subprocess
import sys
import tempfile
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


def _guess_mime(p: Path) -> str:
    mime, _ = mimetypes.guess_type(p.name)
    if mime:
        return mime
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    }.get(p.suffix.lower(), "application/octet-stream")


def _clipboard_to_data_uri() -> str:
    """Read the current clipboard image and return it as a data: URI.

    Uses pngpaste (macOS) to grab whatever image is on the clipboard.
    Raises RuntimeError if pngpaste is not installed or clipboard has no image.
    """
    pngpaste = shutil.which("pngpaste")
    if not pngpaste:
        raise RuntimeError(
            "pngpaste is required to use 'clipboard' as a reference image.\n"
            "Install it with:  brew install pngpaste"
        )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = Path(f.name)
    try:
        result = subprocess.run([pngpaste, str(tmp)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"No image found on clipboard (pngpaste said: {result.stderr.strip() or 'no output'}). "
                "Copy an image first (Cmd+C), then retry."
            )
        b64 = base64.b64encode(tmp.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    finally:
        tmp.unlink(missing_ok=True)


def _load_reference(path_or_url: str) -> str:
    """Resolve a reference input to a data: URI / passthrough URL.

    Used for IMAGE generation (multimodal chat completions), where OpenAI-
    compatible gateways accept data: URIs in image_url content blocks.

    Accepts:
      - "clipboard"     → reads current clipboard image via pngpaste (macOS)
      - http(s):// URLs → returned as-is
      - data:... URIs   → returned as-is
      - local file paths → read bytes, base64-encode → data: URI
    """
    s = path_or_url.strip()
    if s.lower() == "clipboard":
        return _clipboard_to_data_uri()
    if s.startswith(("http://", "https://", "data:")):
        return s
    p = Path(_expand(s))
    if not p.is_file():
        raise RuntimeError(f"Reference not found at {p} (give an absolute path, http(s):// URL, data: URI, or 'clipboard')")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{_guess_mime(p)};base64,{b64}"


def _load_reference_for_veo(path_or_url: str) -> dict[str, str]:
    """Resolve a reference image for VIDEO generation (Vertex Veo via LiteLLM).

    Vertex Veo rejects data: URIs and http(s):// URLs — it requires either a
    GCS URI or an inline base64 dict. LiteLLM mirrors that constraint at the
    /v1/videos endpoint. Accepted shapes:

      - "clipboard"                       → reads clipboard image via pngpaste
      - gs://bucket/path                  → {"gcsUri": "gs://..."}
      - http(s):// URL                    → fetch bytes ourselves, then b64
      - data:image/...;base64,...         → split, return dict
      - local file path                   → read bytes, b64-encode
    """
    s = path_or_url.strip()
    if s.lower() == "clipboard":
        data_uri = _clipboard_to_data_uri()
        # Convert the data: URI we just built into the Veo dict shape.
        head, payload = data_uri.split(",", 1)
        mime = head.split(":", 1)[1].split(";", 1)[0]
        return {"bytesBase64Encoded": payload, "mimeType": mime}
    if s.startswith("gs://"):
        return {"gcsUri": s}

    if s.startswith("data:"):
        # data:<mime>;base64,<payload>
        try:
            head, payload = s.split(",", 1)
            mime = head.split(":", 1)[1].split(";", 1)[0] or "image/png"
            return {"bytesBase64Encoded": payload, "mimeType": mime}
        except (IndexError, ValueError) as e:
            raise RuntimeError(f"Malformed data: URI: {e}") from e

    if s.startswith(("http://", "https://")):
        # Veo can't fetch external URLs itself; pull the bytes here and inline.
        with httpx.Client(timeout=60) as c:
            r = c.get(s)
            r.raise_for_status()
            mime = r.headers.get("content-type", "image/png").split(";")[0]
            b64 = base64.b64encode(r.content).decode("ascii")
            return {"bytesBase64Encoded": b64, "mimeType": mime}

    p = Path(_expand(s))
    if not p.is_file():
        raise RuntimeError(f"Reference not found at {p} (give an absolute path, http(s):// URL, gs:// URI, or data: URI)")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"bytesBase64Encoded": b64, "mimeType": _guess_mime(p)}


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


# Veo single-shot generation tops out at 8 seconds. LiteLLM's
# /v1/videos/extensions route exists but Vertex AI implementation is NOT
# wired upstream ("video extension is not supported for Vertex AI"), so
# we can't chain via the gateway. Instead we frame-chain client-side:
#   1. Generate base 8-sec clip
#   2. Extract its last frame with ffmpeg
#   3. Use that frame as input_reference for the next 8-sec clip
#   4. Concatenate all chunks with ffmpeg
# Result: a single continuous MP4 with visual continuity across joins.
VEO_CHUNK_MAX = 8
VEO_CHUNK_MIN = 5  # Veo rejects < 5 sec per chunk


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _extract_last_frame(video_path: Path, out_png: Path) -> None:
    """Pull the final frame of an MP4 out as a PNG, for use as the
    input_reference seed of the next chunk."""
    # Probe duration so we can ask ffmpeg for that exact second.
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    seek = max(0.0, float(dur) - 0.1)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-ss", f"{seek:.2f}", "-i", str(video_path),
         "-frames:v", "1", str(out_png)],
        check=True,
    )


def _concat_videos(chunks: list[Path], out_path: Path) -> None:
    """Stream-copy concat (no re-encode) so quality is preserved. Veo chunks
    share codec + dimensions, so concat demuxer works without filter graph."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for c in chunks:
            # ffmpeg concat list: escape single quotes
            f.write(f"file '{str(c).replace(chr(39), chr(92) + chr(39))}'\n")
        list_path = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", str(out_path)],
            check=True,
        )
    finally:
        os.unlink(list_path)


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


async def _gen_one_video_chunk(
    client: httpx.AsyncClient,
    *,
    base: str,
    headers: dict[str, str],
    model: str,
    prompt: str,
    seconds: int,
    size: str | None,
    reference_image_dict: dict[str, str] | None,
    deadline: float,
) -> bytes:
    """Single Veo generation: POST → poll → GET content. Returns raw MP4 bytes."""
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "seconds": seconds}
    if size:
        payload["size"] = size
    if reference_image_dict:
        # Vertex Veo expects a dict here: {bytesBase64Encoded, mimeType} or {gcsUri}.
        payload["input_reference"] = reference_image_dict
    resp = await client.post(f"{base}/v1/videos", json=payload, headers=headers)
    resp.raise_for_status()
    video_id = resp.json()["id"]
    await _poll_video_job(client, base=base, headers=headers, job_id=video_id, deadline=deadline)
    cr = await client.get(f"{base}/v1/videos/{video_id}/content", headers=headers)
    cr.raise_for_status()
    return cr.content


async def _auto_split_scenes(
    cfg: dict[str, str],
    *,
    overall_prompt: str,
    n: int,
    chunk_secs: list[int],
) -> list[str]:
    """Ask the gateway's text model to break a single user request into N
    continuous scenes — one prompt per chunk. Falls back to repeating the
    original prompt across all chunks if the splitter errors or returns
    something un-parseable. Cheap call (~1¢) compared to Veo (~$1+ per chunk).
    """
    import json as _json

    splitter_model = cfg.get("DEFAULT_TEXT_MODEL", "").strip() or "gemini-flash"
    chunk_layout = ", ".join(f"scene {i+1}: {s}s" for i, s in enumerate(chunk_secs))
    splitter_prompt = (
        f"Break this video generation request into exactly {n} continuous scenes "
        f"that visually flow into each other. The chunks will be stitched together, "
        f"with each scene seeded from the LAST FRAME of the previous one — so scene "
        f"N+1 must look like it could plausibly start exactly where scene N ended. "
        f"Scene layout: {chunk_layout}. "
        f"Output ONLY a JSON array of {n} strings — no markdown, no fences, no "
        f"explanation, no surrounding prose. Each string is a self-contained Veo "
        f"prompt describing what happens in that scene's window.\n\n"
        f"Request: {overall_prompt}"
    )
    try:
        raw = await call_chat(
            cfg, prompt=splitter_prompt, model=splitter_model,
            system="You are a video director planning continuous scene transitions. Reply with valid JSON only.",
        )
        # Strip common LLM noise: markdown fences, leading/trailing prose.
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            # Drop possible "json\n" language hint
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
            text = text.rstrip("`").strip()
        # Find the first '[' and last ']' to be forgiving.
        lo, hi = text.find("["), text.rfind("]")
        if lo >= 0 and hi > lo:
            text = text[lo : hi + 1]
        scenes = _json.loads(text)
        if isinstance(scenes, list) and len(scenes) == n and all(isinstance(s, str) and s.strip() for s in scenes):
            return [s.strip() for s in scenes]
    except Exception:
        pass
    # Fallback: same prompt across all chunks. Boring but functional.
    return [overall_prompt] * n


async def call_video(
    cfg: dict[str, str],
    *,
    prompt: str,
    model: str,
    duration_sec: int,
    aspect_ratio: str | None,
    raw: bool,
    reference_image: str | None = None,
    scene_prompts: list[str] | None = None,
    auto_scene_split: bool = True,
) -> dict[str, Any]:
    """Generate a video clip. Two regimes:

      duration_sec ≤ 8  → one Veo call, async (POST → poll → content)
      duration_sec  > 8 → frame-chain client-side:
          * Generate base 8-sec clip
          * Extract last frame with ffmpeg
          * Use it as input_reference for next 8-sec clip
          * Concatenate all chunks (stream-copy, no re-encode)

    Veo's gateway-side extension route would be the cleaner approach, but
    LiteLLM hasn't wired it up for Vertex AI yet (errors with "video
    extension is not supported for Vertex AI"). Frame-chain works on any
    Veo backend and gives smoother joins than independent clips.

    Returns {'_video_bytes': bytes, '_seconds': int, '_chunks': int}.
    """
    full_prompt = _brand_prefix(cfg, raw) + prompt
    headers = _headers(cfg)
    base = cfg["GATEWAY_BASE_URL"].rstrip("/")
    job_timeout = int(cfg.get("VIDEO_TIMEOUT", "600"))

    total = max(VEO_CHUNK_MIN, min(int(duration_sec), 60))
    size = _aspect_to_size(aspect_ratio)
    initial_ref = _load_reference_for_veo(reference_image) if reference_image else None

    # Single-chunk fast path
    if total <= VEO_CHUNK_MAX:
        async with httpx.AsyncClient(timeout=job_timeout) as client:
            deadline = time.time() + job_timeout
            mp4 = await _gen_one_video_chunk(
                client, base=base, headers=headers,
                model=model, prompt=full_prompt, seconds=total, size=size,
                reference_image_dict=initial_ref, deadline=deadline,
            )
        return {"_video_bytes": mp4, "_seconds": total, "_chunks": 1}

    # Multi-chunk: needs ffmpeg for frame extraction + concat
    if not _have_ffmpeg():
        raise RuntimeError(
            "ffmpeg + ffprobe required for clips > 8 seconds (frame-chain assembly). "
            "Install via:  brew install ffmpeg  (macOS)  /  sudo apt install ffmpeg  (Linux)"
        )

    # Plan: chunks of 8 sec each. Last chunk may be 5-8 sec; if remainder < 5,
    # bump it to 5 and overshoot — Veo rejects sub-5 chunks.
    chunk_sizes: list[int] = []
    remaining = total
    while remaining > 0:
        if remaining >= VEO_CHUNK_MAX:
            chunk_sizes.append(VEO_CHUNK_MAX)
            remaining -= VEO_CHUNK_MAX
        elif remaining >= VEO_CHUNK_MIN:
            chunk_sizes.append(remaining)
            remaining = 0
        else:
            chunk_sizes.append(VEO_CHUNK_MIN)
            remaining = 0  # tolerates a slight overshoot

    n_chunks = len(chunk_sizes)

    # Per-chunk prompts: caller-supplied wins, then auto-split, then same prompt.
    if scene_prompts is not None:
        if len(scene_prompts) != n_chunks:
            raise RuntimeError(
                f"scene_prompts has {len(scene_prompts)} entries but duration_sec={total} "
                f"plans as {n_chunks} chunks ({chunk_sizes}). Pass exactly {n_chunks} prompts, "
                f"omit scene_prompts to let the server auto-split, or set auto_scene_split=False "
                f"to repeat the main prompt across chunks."
            )
        chunk_prompts = [_brand_prefix(cfg, raw) + p for p in scene_prompts]
    elif auto_scene_split:
        auto = await _auto_split_scenes(cfg, overall_prompt=prompt, n=n_chunks, chunk_secs=chunk_sizes)
        chunk_prompts = [_brand_prefix(cfg, raw) + p for p in auto]
    else:
        chunk_prompts = [full_prompt] * n_chunks

    chunk_paths: list[Path] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="veltria-genmedia-"))
    try:
        async with httpx.AsyncClient(timeout=job_timeout) as client:
            current_ref = initial_ref
            for i, sec in enumerate(chunk_sizes):
                deadline = time.time() + job_timeout
                mp4 = await _gen_one_video_chunk(
                    client, base=base, headers=headers,
                    model=model, prompt=chunk_prompts[i], seconds=sec, size=size,
                    reference_image_dict=current_ref, deadline=deadline,
                )
                chunk_path = tmpdir / f"chunk-{i:02d}.mp4"
                chunk_path.write_bytes(mp4)
                chunk_paths.append(chunk_path)
                # Seed the NEXT chunk with this one's last frame (Veo-shaped dict).
                if i + 1 < n_chunks:
                    frame_path = tmpdir / f"seed-{i+1:02d}.png"
                    _extract_last_frame(chunk_path, frame_path)
                    current_ref = _load_reference_for_veo(str(frame_path))

        final_path = tmpdir / "final.mp4"
        _concat_videos(chunk_paths, final_path)
        bytes_out = final_path.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "_video_bytes": bytes_out,
        "_seconds": sum(chunk_sizes),
        "_chunks": n_chunks,
        "_chunk_prompts": chunk_prompts,
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
                        "description": "Optional list of reference inputs. Each entry is an absolute file path on disk (e.g. /Users/.../photo.png), an http(s):// URL, a data: URI, or the magic string 'clipboard' to read whatever image the user has copied (Cmd+C) on macOS. When provided, routes via chat/completions for multimodal generation.",
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
                "Generate a video clip via your gateway. Async flow: POST the "
                "job, poll until ready, fetch the MP4. Model names must match "
                "what your gateway exposes ('veo-3', 'veo-2', 'sora-2', "
                "'runway-gen3'). "
                "Veo caps each generation at 8 seconds. For longer clips the "
                "server chains 8-sec chunks client-side, seeding each new "
                "chunk with the previous chunk's last frame (via ffmpeg) and "
                "concatenating the final MP4 — so a 24-sec request runs as "
                "three back-to-back Veo calls with visual continuity at every "
                "join. Hard cap 60 sec. Each chunk is a billable Veo call "
                "(~$0.10–0.35/sec), so a 30-sec clip is ~4× the cost of an "
                "8-sec clip. "
                "Per-chunk scene control: by default, when duration_sec > 8, "
                "the server asks the gateway's text model to break your prompt "
                "into N continuous scenes (one per chunk) for narrative flow. "
                "Override with explicit `scene_prompts` (list of N strings, "
                "one per chunk) for full control, or set "
                "`auto_scene_split=false` to repeat the same prompt across "
                "all chunks. "
                "Pass `reference_image` to animate a still photo (image-to-"
                "video / first-frame conditioning). "
                "Each chunk takes 30–90 sec; long clips multiply that — "
                "expect 3–8 min wall-clock for a 24-sec clip. Patient mode."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The overall video request. For clips ≤8s this is sent directly to Veo. For longer clips it's used as a directive for the scene-splitter unless you also pass scene_prompts.",
                    },
                    "model": {"type": "string", "description": "Model name as configured on your gateway"},
                    "duration_sec": {
                        "type": "integer",
                        "description": "Target clip length in seconds. Clamped to 5–60. Clips > 8s are auto-chained as 8-sec chunks.",
                        "default": 5,
                        "minimum": 5,
                        "maximum": 60,
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Convenience aspect: '16:9', '9:16', '1:1', '4:3', '3:4' — auto-converted to size. Or pass raw 'WxH' (e.g. '1280x720').",
                    },
                    "reference_image": {
                        "type": "string",
                        "description": "Optional first-frame seed for the FIRST chunk. File path, http(s):// URL, data: URI, or 'clipboard' to use the image currently copied on macOS. Subsequent chunks always seed from the previous chunk's last frame.",
                    },
                    "scene_prompts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional per-chunk prompts for narrative reels. Length must equal the chunk count derived from duration_sec (one prompt per 8-sec chunk; final chunk may be 5–7s). When given, takes precedence over the main prompt for chunk generation. Use when you want explicit scene direction (e.g. ['product on white', 'zoom out to kitchen', 'hand lifts mug']).",
                    },
                    "auto_scene_split": {
                        "type": "boolean",
                        "default": True,
                        "description": "When duration_sec > 8 and scene_prompts is not given, asks the gateway's text model to break the main prompt into N continuous scenes for narrative flow. Set false to repeat the same prompt across all chunks (continuous visual, no narrative).",
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
        scene_prompts = arguments.get("scene_prompts")
        if scene_prompts is not None and not isinstance(scene_prompts, list):
            return [TextContent(type="text", text="❌ scene_prompts must be an array of strings")]
        try:
            result = await call_video(
                cfg,
                prompt=arguments["prompt"],
                model=arguments.get("model", cfg.get("DEFAULT_VIDEO_MODEL", "")),
                duration_sec=int(arguments.get("duration_sec", 5)),
                aspect_ratio=arguments.get("aspect_ratio"),
                raw=bool(arguments.get("raw", False)),
                reference_image=arguments.get("reference_image"),
                scene_prompts=scene_prompts,
                auto_scene_split=bool(arguments.get("auto_scene_split", True)),
            )
        except httpx.HTTPStatusError as e:
            return [TextContent(type="text", text=f"❌ Gateway error {e.response.status_code}: {e.response.text[:500]}")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ {type(e).__name__}: {e}")]
        paths = save_video_result(cfg, result=result, prompt=arguments["prompt"], output_dir=arguments.get("output_dir"))
        if not paths:
            return [TextContent(type="text", text="⚠️ Gateway returned no video data. Full response:\n" + str(result)[:1500])]
        dur = time.time() - started
        msg = f"✅ Generated {len(paths)} video(s) in {dur:.1f}s ({result.get('_seconds')}s, {result.get('_chunks')} chunk(s)):\n" + "\n".join(paths)
        chunk_prompts = result.get("_chunk_prompts")
        if chunk_prompts and len(chunk_prompts) > 1:
            msg += "\n\nScenes used:\n" + "\n".join(f"  {i+1}. {p}" for i, p in enumerate(chunk_prompts))
        return [TextContent(type="text", text=msg)]

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
