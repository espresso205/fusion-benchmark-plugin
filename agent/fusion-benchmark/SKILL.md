---
name: fusion-benchmark
description: Use when running a Fusion CAD benchmark through the portable fusion-benchmark plugin, connecting a supported CLI to its MCP service, or inspecting benchmark progress and authorized results.
---

# Fusion benchmark

Locate the extracted bundle and invoke its `fusion-benchmark.cmd`. The complete portable release includes Python and evaluator dependencies. Run `doctor` first; consult `--help` for command arguments. Follow [the portable usage guide](../../docs/PORTABLE_PLUGIN_USAGE.md) for CLI connection and the administrator's run configuration. `connect` registers MCP configuration; install or reference this skill separately using the host's supported skill mechanism.

Use the registered benchmark MCP tools when available. Otherwise call the launcher through the CLI's shell tool. MCP registration exposes service tools; it does not automatically create native subagents in every CLI. Supported adapter capabilities are reported by the service.

For a requested benchmark run, the trusted coordinator creates a new run and a fresh candidate session for each question. Each candidate receives that question's public TASK/DXF, its submission directory and modeling instructions. The candidate submits the requested F3D. The trusted coordinator freezes the submission, reopens it in Fusion, exports STEP and invokes the bundled evaluator. A submitted filename alone is not proof of successful trusted export or scoring.

Keep GT, annotations, prior answers and scores in the trusted coordinator's private configuration. Do not include them in candidate prompts or use intermediate scores to retry a question. Separate sessions under one operating-system account do not prevent file access; use the configured isolation policy and report its actual level.

Poll `status` for the run identifier returned by the service. Report completed, failed, blocked and unknown stages using their recorded evidence. An unknown Fusion request or timeout requires operator investigation; do not automatically retry the same attempt or clear its lock. `results` is for authorized coordinator access. A command returning JSON does not imply that every question has finished or is eligible for ranking.

When reporting a run, include its identifier, completed-question count, export/scoring coverage, failures requiring action and artifact location. Distinguish portable dependency checks, synthetic geometry tests and actual Fusion/model execution.
