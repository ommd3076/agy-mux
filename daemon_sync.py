"""Explicit, one-shot Git validation and sync helper; no unattended daemon."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed with {result.returncode}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / ".git").exists():
        parser.error(f"{root} is not a Git worktree")
    status = run_git(root, "status", "--porcelain")
    if not status:
        print("Working tree is clean; nothing to sync.")
        return 0
    print(status)
    if args.dry_run:
        print("Dry run: no files staged, committed, or pushed.")
        return 0
    parser.error("Automatic staging and pushing are disabled. Review the listed paths and run explicit Git commands.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
