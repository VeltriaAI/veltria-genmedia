# Troubleshooting

## "Authentication failed (HTTP 401)"

Your virtual key is wrong, expired, or revoked.

1. Open `~/.config/veltria-genmedia/gateway.env`
2. Confirm `GATEWAY_API_KEY` is exactly the value your admin sent — no trailing whitespace, no extra quotes
3. If still failing, ask your gateway admin for a fresh key
4. Also confirm `AUTH_HEADER` matches what your gateway expects (`x-api-key` for most LiteLLM setups; `authorization` for OpenAI-style Bearer)

## "Budget exceeded (HTTP 402)"

You hit your monthly cap (configured at the gateway, not in this client).

- Ask your gateway admin to top up your budget, or wait until the next reset window
- Most gateways send Teams/Slack/email alerts before the hard stop — check your inbox

## "Model not allowed (HTTP 403)"

You tried to call a model that isn't on your allowlist (configured per-key at the gateway).

- Run `./scripts/verify.sh` — it lists every model your key CAN call
- Ask your gateway admin if you genuinely need a different model

## "Too many requests (HTTP 429)"

Over your per-minute rate limit. Slow down — usually means a script is looping.

## Claude doesn't see the `gen_image` tool

1. Did you restart your client after install?
   - Claude Desktop: `Cmd+Q` then reopen (not just close window)
   - Claude Code: new session / restart terminal
2. Check the MCP server is wired:
   ```bash
   # Claude Desktop
   jq '.mcpServers."veltria-genmedia"' "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
   # Claude Code
   jq '.mcpServers."veltria-genmedia"' "$HOME/.claude.json"
   ```
   Should print the server block, not `null`.
3. Check Python can run the server:
   ```bash
   python3 ~/skills/veltria-genmedia/mcp/server.py --help 2>&1 | head -5
   ```
   If it crashes on import, re-run `./scripts/install.sh`.

## "Gateway returned no image data"

Rare. Usually:
- A safety filter blocked the prompt (no error, just empty result). Try a simpler prompt.
- Upstream provider hiccup. Wait 1 min, retry.

Your gateway admin can see these in gateway logs.

## Video generation hanging

Some video providers take minutes. The MCP server has a 10-minute timeout (`VIDEO_TIMEOUT=600`). If it actually hangs past that, kill the process and try a shorter clip.

## Output files don't appear in the expected folder

Check `~/.config/veltria-genmedia/gateway.env`:
```bash
grep OUTPUT_DIR ~/.config/veltria-genmedia/gateway.env
```

Confirm the paths exist and you have write permission.

## Permission denied on `mcp/server.py`

```bash
chmod +x ~/skills/veltria-genmedia/mcp/server.py
```

## Slow first call after restart

Normal. Cold start ~30 sec, warm subsequent calls 5–10 sec.

## "Permission denied" reading config

```bash
chmod 600 ~/.config/veltria-genmedia/gateway.env
ls -la ~/.config/veltria-genmedia/gateway.env
```

Should show `-rw-------` (owner read/write only).

## Need to start over

```bash
rm ~/.config/veltria-genmedia/gateway.env
./scripts/install.sh
```

The install backs up any prior Claude config before overwriting — look for `.backup-*` files next to your `claude_desktop_config.json` if you need to revert.

## Where do I get help?

- **Bugs / feature requests:** open an issue at https://github.com/VeltriaAI/veltria-genmedia/issues
- **Gateway-side questions** (budgets, models, keys): your gateway admin
