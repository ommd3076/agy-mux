"""Validate, commit, and push repository changes on a guarded interval."""

from __future__ import annotations

import argparse
import ast
import hashlib
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path

LOG = logging.getLogger("agy_sync")
SECRET_NAME = re.compile(r"(^\.env($|\.)|(^|/)(id_rsa|id_ed25519|credentials\.json)$|\.(pem|p12|pfx|key)$|(^|/)(cookies?|secrets?)\.(json|txt)$)", re.I)
SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|\bASIA[0-9A-Z]{16}\b|"
    r"(?:sessionid|session_token|auth_token|access_token|refresh_token)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{20,}",
    re.IGNORECASE,
)
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
MAX_SCAN_BYTES = 8 * 1024 * 1024


class SyncError(RuntimeError):
    """A validation or Git operation failed safely."""


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise SyncError(result.stderr.strip() or f"git {' '.join(args)} exited with {result.returncode}")
    return result


def changed_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--modified", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=False,
    )
    if result.returncode:
        raise SyncError("Could not enumerate candidate files for secret scanning")
    return [root / item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item]


def scan_secrets(root: Path) -> None:
    findings: list[str] = []
    for path in changed_paths(root):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in Path(relative).parts) or not path.is_file():
            continue
        if SECRET_NAME.search(relative):
            findings.append(f"sensitive filename: {relative}")
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                findings.append(f"file exceeds secret scan size limit: {relative}")
                continue
            content = path.read_bytes()
            if b"\0" in content:
                continue
            if SECRET_CONTENT.search(content.decode("utf-8", errors="replace")):
                findings.append(f"credential-like content: {relative}")
        except OSError as exc:
            findings.append(f"could not scan {relative}: {exc}")
    if findings:
        raise SyncError("Secret scan rejected this sync: " + "; ".join(findings))


def validate(root: Path) -> None:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"manifest.json validation failed: {exc}") from exc
    entrypoint = manifest.get("entrypoint", "").split(":", 1)[0]
    if not entrypoint or not (root / (entrypoint.replace(".", os.sep) + ".py")).is_file():
        raise SyncError("manifest entrypoint does not point to a Python module")
    requirements_path = root / "requirements.txt"
    if not requirements_path.is_file():
        raise SyncError("requirements.txt is missing")
    req_text = requirements_path.read_text(encoding="utf-8").lower()
    for dep in manifest.get("dependencies", {}).keys():
        if dep.lower() not in req_text:
            raise SyncError(f"manifest dependency {dep!r} missing from requirements.txt")
    constitution_path = root / "constitution.md"
    if not constitution_path.is_file():
        raise SyncError("constitution.md is missing")
    for source in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in source.relative_to(root).parts):
            continue
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise SyncError(f"Python syntax validation failed for {source.relative_to(root)}: {exc}") from exc
    diff = git(root, "diff", "--check", check=False)
    staged = git(root, "diff", "--cached", "--check", check=False)
    if diff.returncode or staged.returncode:
        raise SyncError("git diff --check rejected whitespace errors")


def acquire_lock(root: Path):
    identity = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f"agy-sync-{identity}.lock"
    handle = lock_path.open("a+b")
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise SyncError("another sync daemon holds the repository lock")
    else:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SyncError("another sync daemon holds the repository lock")
    return handle


def run_cycle(root: Path) -> bool:
    branch = git(root, "branch", "--show-current").stdout.strip()
    if branch != "main":
        raise SyncError(f"refusing to sync branch {branch!r}; expected 'main'")
    remote = git(root, "remote", "get-url", "origin").stdout.strip()
    if not remote:
        raise SyncError("origin remote has no URL")
    validate(root)
    status = git(root, "status", "--porcelain").stdout
    if status:
        scan_secrets(root)
        git(root, "add", ".")
        validate(root)
        staged = git(root, "diff", "--cached", "--name-only").stdout.strip()
        if staged:
            stamp = dt.datetime.now().astimezone().strftime("%H:%M")
            message = f"feat(core): autonomous cadence cycle [{stamp}]"
            git(root, "commit", "-m", message)
            LOG.info("Committed validated changes: %s", message)
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if upstream.returncode:
        git(root, "push", "--set-upstream", "origin", "main")
        LOG.info("Initial upstream push completed to %s", remote)
        return True
    counts = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").stdout.split()
    if len(counts) != 2:
        raise SyncError("could not determine upstream divergence")
    behind, ahead = (int(value) for value in counts)
    if behind:
        raise SyncError(f"origin/main is {behind} commit(s) ahead; refusing an implicit merge or rebase")
    if ahead:
        git(root, "push", "origin", "main")
        LOG.info("Pushed %d validated commit(s) to origin/main", ahead)
        return True
    LOG.info("Repository clean and synchronized")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interval-minutes", type=int, default=16)
    parser.add_argument("--once", action="store_true", help="run one validated sync cycle")
    parser.add_argument("--dry-run", action="store_true", help="validate and report without staging or publishing")
    args = parser.parse_args()
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be positive")
    root = args.root.resolve()
    if not (root / ".git").exists():
        parser.error(f"{root} is not a Git worktree")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        lock = acquire_lock(root)
    except SyncError as exc:
        parser.exit(2, f"Sync daemon error: {exc}\n")
    with lock:
        while True:
            failed = False
            try:
                if args.dry_run:
                    validate(root)
                    scan_secrets(root)
                    LOG.info("Validation and secret scan passed; dry run made no repository changes")
                else:
                    run_cycle(root)
            except (SyncError, OSError, subprocess.SubprocessError) as exc:
                LOG.error("Sync cycle failed safely: %s", exc)
                failed = True
            if args.once or args.dry_run:
                return 1 if failed else 0
            time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
