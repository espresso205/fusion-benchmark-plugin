# Claude Code discovery template

PNG/JPEG and multiple attachments require the unified gateway and an explicit `inputs.json` (see `docs/INPUT_FORMATS.md`). Verify the selected model with the billable `visual-check` command; CLI discovery alone does not prove visual support.

Use `fusion-benchmark.cmd connect --cli claude --target <project>/.mcp.json --config <pipeline.json> --dry-run`, then omit `--dry-run` to register. The connection command safely merges the supported `mcpServers` entry. Install or reference `agent/fusion-benchmark/SKILL.md` according to the host's skill mechanism. Keep the recipient's authentication in their own CLI environment.

The MCP entry starts the extracted `runtime/python/python.exe` with an argument array containing `-B`, the absolute `fusion-benchmark-bootstrap.py` path, `serve`, `--config` and the administrator's configuration. Use generated paths instead of manually substituting the maintainer's machine paths.

MCP discovery exposes benchmark tools. It does not prove that Claude Code's candidate-execution adapter is installed, authenticated or isolation-capable. Use `doctor` and the service's capability response to determine supported execution modes.
