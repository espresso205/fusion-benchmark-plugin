"""Compatibility entry point. Both CLI forms use the same implementation."""
from scheduler.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())