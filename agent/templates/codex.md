# Codex discovery template

Use `fusion-benchmark.cmd connect --cli codex --target <project>/.codex/config.toml --config <pipeline.json> --dry-run`, then omit `--dry-run` to register. The connection command safely merges the supported TOML MCP server entry. Install or reference `agent/fusion-benchmark/SKILL.md` according to the host's skill mechanism. Do not replace unrelated Codex settings or copy credentials into a distributable bundle.

The local MCP entry uses a command plus an argument array: the extracted `runtime/python/python.exe`, followed by `-B`, the absolute `fusion-benchmark-bootstrap.py` path, `serve`, `--config` and the administrator's configuration. Use the generated configuration to preserve Windows path quoting.

A discovered benchmark MCP server and a working native candidate adapter are separate capabilities. Check `doctor` and advertised adapter support before starting questions. Do not claim unattended Codex candidate execution merely because MCP discovery works.
