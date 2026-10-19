"""Exercise the real CLI with synthetic bytes; never connect to Fusion or read GT."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=BASE / "runs" / "offline_demos")
    args = parser.parse_args()
    commands = []

    def cli(*arguments, expected=0):
        result = subprocess.run([sys.executable, "-m", "scheduler", *map(str, arguments)],
                                cwd=BASE, text=True, encoding="utf-8", capture_output=True, timeout=30)
        commands.append({"arguments": list(map(str, arguments)), "exit_code": result.returncode})
        if result.returncode != expected:
            raise RuntimeError(f"unexpected exit {result.returncode} for {arguments}: {result.stderr} {result.stdout}")
        return result.stdout.strip()

    def write_new(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)

    run = Path(cli("create-run", args.root))
    source = run / "synthetic_inputs"
    write_new(source / "TASK.md", "SYNTHETIC FILE CONTRACT DEMO ONLY. No CAD modeling.\n")
    write_new(source / "drawing.dxf", "NOT A VALID DXF: synthetic file integrity fixture.\n")
    cli("register-case", run, "DEMO01", "--task", source / "TASK.md", "--drawing", source / "drawing.dxf")
    cli("transition", run, "DEMO01", "case_queued")
    cli("transition", run, "DEMO01", "modeling")
    candidate = run / "candidate" / "DEMO01"
    write_new(candidate / "submission.f3d", "NOT A VALID F3D: synthetic file integrity fixture.\n")
    write_new(candidate / "dependency_manifest.json", json.dumps({
        "schema_version": 1, "case_id": "DEMO01", "attempt_id": "attempt-1",
        "inspection_method": "Synthetic fixture with no external resources; not a Fusion inspection",
        "reviewer": "scheduler_offline_demo.py", "dependencies": [],
    }, indent=2))
    cli("transition", run, "DEMO01", "artifact_staged")
    manifest = Path(cli("freeze-candidate", run, "DEMO01"))
    verified = json.loads(cli("verify", run.parent, run.name, manifest))
    if verified["valid"] is not True:
        raise RuntimeError("expected the synthetic file hashes to match")
    cli("verify", run.parent, run.name, run / "missing.json", expected=2)

    # Deliberate mutation and exact restoration are confined to this new synthetic run.
    f3d = candidate / "submission.f3d"
    original = f3d.read_bytes()
    f3d.write_bytes(b"INTENTIONAL SYNTHETIC TAMPER\n")
    invalid = json.loads(cli("status", run, expected=2))
    if invalid["cases"]["DEMO01"]["submission_verified"]:
        raise RuntimeError("tamper was not detected")
    f3d.write_bytes(original)
    final = json.loads(cli("status", run))
    if not final["all_submissions_verified"] or any(final[key] for key in ("all_artifacts_frozen", "batch_complete", "rank_eligible")):
        raise RuntimeError("incorrect offline completion flags")
    result = {"synthetic_only": True, "cad_validation_performed": False, "run": str(run),
              "checks_passed": ["public input snapshot", "candidate manifest", "invalid verify exit 2",
                                "tamper detected on status", "exact restoration revalidated", "live gates remain false"],
              "commands": commands}
    write_new(run / "demo_result.json", json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
