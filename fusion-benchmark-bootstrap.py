"""Stable file entry point for CLI registration and relocated portable bundles."""
from pathlib import Path
import sys


for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fusion_pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
