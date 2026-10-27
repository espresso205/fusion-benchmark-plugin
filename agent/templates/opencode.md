# OpenCode discovery template

PNG/JPEG and multiple attachments require the unified gateway and an explicit `inputs.json` (see `docs/INPUT_FORMATS.md`). Verify the selected model with the billable `visual-check` command; CLI discovery alone does not prove visual support.

Use `fusion-benchmark.cmd connect --cli opencode --target <project>/opencode.json --config <pipeline.json> --dry-run`, then omit `--dry-run` to register. The connection command safely merges the supported OpenCode `mcp` configuration. Install or reference `agent/fusion-benchmark/SKILL.md` according to the host's skill mechanism. Do not copy the maintainer's global configuration, accounts or model credentials.

The registered local MCP command starts the extracted bundle's `runtime/python/python.exe`, passing `-B`, the absolute `fusion-benchmark-bootstrap.py` path, `serve`, `--config` and the administrator's configuration. The generated entry preserves the actual extraction path, including Chinese characters and spaces. OpenCode configuration must be strict JSON, not JSONC.

Run `fusion-benchmark.cmd doctor` before a benchmark. OpenCode candidate execution is available only when its adapter and Fusion bridge pass the configured checks. Model selection and credentials belong to the recipient's environment.
