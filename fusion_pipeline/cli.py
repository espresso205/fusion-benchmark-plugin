"""Portable entry point shared by MCP registration and local command use."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ablation"]:
        # Offline analysis shares the evaluator CLI and needs no service/config.
        from analyze_ablation import main as analyze_ablation_main
        return analyze_ablation_main(argv[1:])

    parser = argparse.ArgumentParser(description="Unified Fusion benchmark management")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ablation", help="Analyze saved results without Fusion or configuration")
    for name in ("serve", "doctor"):
        item = commands.add_parser(name)
        item.add_argument("--config", type=Path)
    connect = commands.add_parser("connect")
    connect.add_argument("--cli", choices=("opencode", "codex", "claude"), required=True)
    connect.add_argument("--target", type=Path, required=True, help="Explicit CLI configuration file; JSON for OpenCode/Claude, TOML for Codex")
    connect.add_argument("--config", type=Path, required=True, help="Maintainer pipeline configuration")
    connect.add_argument("--dry-run", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--preflight-only", action="store_true")
    job = commands.add_parser("_run-job", help=argparse.SUPPRESS)
    job.add_argument("--job", type=Path, required=True)
    job.add_argument("--preflight-only", action="store_true")
    gateway = commands.add_parser("worker", help="Attempt-scoped worker MCP endpoint")
    gateway.add_argument("--contract", type=Path, required=True)
    for name in ("status", "results", "cancel"):
        item = commands.add_parser(name)
        item.add_argument("--config", type=Path, required=True)
        item.add_argument("run_id")
    args = parser.parse_args(argv)
    from .service import ManagementService, serve, run_job
    if args.command == "serve":
        serve(ManagementService(args.config))
        return 0
    if args.command == "worker":
        from .worker_gateway import WorkerGateway
        serve(WorkerGateway(args.contract))
        return 0
    if args.command == "_run-job":
        return run_job(args.job, args.preflight_only)
    if args.command == "doctor":
        result = ManagementService(args.config).call("preflight", {})
    elif args.command == "connect":
        from .discovery import definition, register
        command = [str(Path(sys.executable).resolve()), "-B", "-m", "fusion_pipeline.cli", "serve", "--config", str(args.config.resolve())]
        # Registration must work from any cwd, including the recipient's workspace.
        bootstrap = Path(__file__).resolve().parents[1] / "fusion-benchmark-bootstrap.py"
        if bootstrap.exists():
            command = [str(Path(sys.executable).resolve()), "-B", str(bootstrap), "serve", "--config", str(args.config.resolve())]
        result = definition(args.cli, command) if args.dry_run else register(args.cli, args.target, command)
    elif args.command == "run":
        from .runner import Trial
        trial = Trial(args.config)
        print(json.dumps({"run": str(trial.run)}, ensure_ascii=False), flush=True)
        return trial.run_all(preflight_only=args.preflight_only)
    else:
        result = ManagementService(args.config).call(args.command, {"run_id": args.run_id})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
