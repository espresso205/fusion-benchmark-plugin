"""CLI for the offline submission ledger; it never launches OpenCode or Fusion."""
import argparse
import json
from pathlib import Path
import sys

from .core import Scheduler


def _load(run):
    # Do not resolve first: load must see and reject symlink/junction components.
    run = run.absolute()
    return Scheduler.load(run.parent, run.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)
    create = subs.add_parser("create-run", help="create a unique run; existing directories are rejected")
    create.add_argument("root", type=Path)
    create.add_argument("run_id", nargs="?")
    status = subs.add_parser("status", help="revalidate evidence and rebuild summary/events from checkpoint")
    status.add_argument("run", type=Path)
    register = subs.add_parser("register-case", help="snapshot this case's public inputs and bind its contract")
    register.add_argument("run", type=Path)
    register.add_argument("case_id")
    register.add_argument("--task", type=Path, required=True)
    register.add_argument("--drawing", type=Path, required=True)
    register.add_argument("--attempt-id", default="attempt-1")
    register.add_argument("--protocol-id", default="fusion-step-adapted-v2.0")
    transition = subs.add_parser("transition", help="record an offline stage; does not execute modeling")
    transition.add_argument("run", type=Path)
    transition.add_argument("case_id")
    transition.add_argument("stage", choices=("case_queued", "modeling", "artifact_staged"))
    freeze = subs.add_parser("freeze-candidate", help="freeze candidate file hashes; does not certify CAD health")
    freeze.add_argument("run", type=Path)
    freeze.add_argument("case_id")
    verify = subs.add_parser("verify", help="verify a previously frozen submission; invalid evidence exits 2")
    verify.add_argument("root", type=Path)
    verify.add_argument("run_id")
    verify.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "create-run":
            print(Scheduler(args.root).create_run(args.run_id))
            return 0
        if args.cmd == "verify":
            scheduler = Scheduler.load(args.root, args.run_id)
            valid = scheduler.verify_artifact_manifest(args.manifest)
            print(json.dumps({"valid": valid, "mode": "offline_file_integrity", "error": scheduler.last_error}, ensure_ascii=False))
            return 0 if valid else 2
        scheduler = _load(args.run)
        if args.cmd == "status":
            report = scheduler.status()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            # Incomplete work is normal. Detected tampering/missing frozen evidence is an error.
            return 2 if any("verification_error" in c for c in report["cases"].values()) else 0
        if args.cmd == "register-case":
            scheduler.register_case(args.case_id, args.attempt_id, args.protocol_id, task=args.task, drawing=args.drawing)
        elif args.cmd == "transition":
            scheduler.transition(args.case_id, args.stage)
        elif args.cmd == "freeze-candidate":
            print(scheduler.freeze_candidate(args.case_id))
            return 0
        print(json.dumps({"run_id": scheduler.run_id, "case_id": args.case_id, "status": scheduler.case_status(args.case_id)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())