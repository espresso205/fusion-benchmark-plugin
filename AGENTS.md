# Repository use

Read [README.md](README.md), [architecture](docs/ARCHITECTURE.md), and [v4 scoring](docs/SCORING_V4.md). Default v4 evaluates rigidly aligned geometry and reports coordinate differences separately. Only shape/dimension/mass count toward the score and coverage; other measurements are diagnostics. Explicit v3 retains historical fixed-world behavior. Never overwrite historical outputs or original/derived evidence.

Use `python -B fusion-benchmark-bootstrap.py` for source checkouts; `fusion-benchmark.cmd` requires the complete portable runtime. The coordinator skill is [agent/fusion-benchmark/SKILL.md](agent/fusion-benchmark/SKILL.md). The candidate receives only the public [modeling skill](agent/fusion-modeling/SKILL.md), its question and its submission directory.

Run `python -B -m unittest discover -s tests -v` for source checks. Tests use synthetic inputs; optional historical regression inputs are supplied through `FUSION_BENCHMARK_REGRESSION_ROOT`. The portable builder and verification commands are in [docs/BUILD.md](docs/BUILD.md).

Keep credentials, local configuration, private references and generated output outside the repository. Use a new output directory per run. Do not overwrite historical submissions or scores. The scoring-core hashes are recorded in `SCORING_SOURCE_MANIFEST.json`; update them deliberately only for a reviewed scoring-source release.

Explicit v5 is documented in docs/SCORING_V5.md and docs/V5_V4_COMPARISON.md. Keep v4 the default. V5 separates candidate basic/assembly checks, frozen reference acceptance and measurements; a missing main measurement or unaccepted reference cannot publish a complete total. Preserve acceptance identity through runner, management and capability studies. Tests include synthetic v5 fixtures and must not launch real model or CAD work.
