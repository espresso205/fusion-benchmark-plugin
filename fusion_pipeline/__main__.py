import argparse
import json
from pathlib import Path

from .runner import Trial


def main():
    parser = argparse.ArgumentParser(description="Unattended internal OpenCode/Fusion trial")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--preflight-only", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run", type=Path)
    args = parser.parse_args()
    if args.command == "status":
        print((args.run / "pipeline-status.json").read_text(encoding="utf-8"))
        return 0
    trial = Trial(args.config)
    print(json.dumps({"run": str(trial.run), "private_run": str(trial.private)}, ensure_ascii=True), flush=True)
    return trial.run_all(preflight_only=args.preflight_only)


if __name__ == "__main__":
    raise SystemExit(main())
