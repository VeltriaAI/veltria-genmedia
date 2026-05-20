# Install — Wizard Walkthrough

Sets up `veltria-genmedia` so Claude Desktop and/or Claude Code can generate images, videos, and text via your OpenAI-compatible gateway.

**Time:** ~5 minutes.

---

## Prerequisites

- **macOS or Linux** (Windows: untested — use WSL2)
- **Python 3.11+** → `brew install python@3.12` (macOS) or your distro's package manager
- **`jq`** → `brew install jq` (macOS) or `apt install jq` (Linux)
- **`curl`** — usually already there
- **`git`** — for the bootstrap clone
- **An MCP-capable client**: [Claude Desktop](https://claude.ai/download) and/or [Claude Code](https://claude.ai/code)

You also need two things from whoever runs your gateway:

1. The **gateway base URL** (e.g. `https://your-gateway.example.com`)
2. A **virtual API key** for you — shared via a secret-sharing tool (1Password, Bitwarden Send, vault, etc.), not plain email

---

## Run the wizard

**Curl one-liner (self-cloning):**

```bash
curl -fsSL https://raw.githubusercontent.com/VeltriaAI/veltria-genmedia/main/scripts/install.sh | bash
```

The script detects it's being piped and clones itself to `~/skills/veltria-genmedia` first, then continues.

**Safer two-step (inspect before running):**

```bash
curl -fsSL https://raw.githubusercontent.com/VeltriaAI/veltria-genmedia/main/scripts/install.sh -o install.sh
less install.sh
bash install.sh
```

**Manual clone:**

```bash
git clone https://github.com/VeltriaAI/veltria-genmedia.git ~/skills/veltria-genmedia
cd ~/skills/veltria-genmedia
./scripts/install.sh
```

**Custom install location** — set `GENMEDIA_INSTALL_DIR`:

```bash
GENMEDIA_INSTALL_DIR=$HOME/tools/genmedia \
  bash <(curl -fsSL https://raw.githubusercontent.com/VeltriaAI/veltria-genmedia/main/scripts/install.sh)
```

You'll be asked five questions:

1. **Gateway base URL** — e.g. `https://your-gateway.example.com`
2. **Virtual API key** — paste it (hidden, won't echo to terminal)
3. **Which client to wire?** — `desktop`, `code`, or `both`. Default `both`.
4. **Image / video output directories** — defaults to `~/Pictures/genmedia` and `~/Movies/genmedia`
5. **Optional brand preset** — a free-form string prepended to every image prompt (leave blank to skip)

The wizard then:

- ✅ Installs Python deps (`httpx`, `mcp`)
- ✅ Writes config to `~/.config/veltria-genmedia/gateway.env` (chmod 600)
- ✅ Creates output directories
- ✅ Adds the MCP server to your Claude config(s)
- ✅ Verifies the gateway is reachable and your key works

Total time: ~30 seconds after you've gathered the URL + key.

---

## After install

**Restart your client(s).** MCP servers only load on launch.

- Claude Desktop: `Cmd+Q` then reopen
- Claude Code: restart your terminal session

In a new chat, try:

> *"Generate a 1:1 image of a forest cabin at sunrise."*

You should see Claude call the `gen_image` tool, then a file path is returned. Open it from your image output directory.

---

## Available tools

| Tool | What it does |
|---|---|
| `gen_image` | One or more images. Brand preset auto-injected if set. |
| `gen_video` | Short video clip (1–30 sec). Expensive — use thoughtfully. |
| `gen_text` | Captions / drafts / summaries via your gateway's chat endpoint. |

You don't have to remember tool names — Claude picks them based on what you ask.

---

## Headless / CI install

```bash
GENMEDIA_GATEWAY_URL=https://your-gateway.example.com \
GENMEDIA_API_KEY=sk-XXXX \
GENMEDIA_CLIENT=both \
./scripts/install.sh --headless
```

Useful for dotfiles bootstrap or reinstalling on a new machine.

---

## Headless verify

```bash
./scripts/verify.sh           # basic auth + models check
./scripts/verify.sh --image   # also does one real image gen (costs ~$0.01)
```

---

## Updating

```bash
cd ~/skills/veltria-genmedia
git pull
./scripts/install.sh --headless   # uses existing config
```

Your key and gateway URL aren't touched — just the MCP server code refreshes.

---

## Rotating your key

If your key is compromised, ask your gateway admin to revoke + issue a new one. Then:

```bash
nano ~/.config/veltria-genmedia/gateway.env
# Replace GATEWAY_API_KEY=… with the new value, save.
```

The MCP server picks up the new key on the **next call** — no restart needed.

---

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).
