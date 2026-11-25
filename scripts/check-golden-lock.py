"""Verify the frozen golden .screen set byte-for-byte against the lock file.

Read-only: compares raw bytes (trailing blanks included) via SHA-256 over the
exact pinned file set. Reports missing, extra, renamed, and modified files.
There is intentionally no update/overwrite flag: the lock only changes by
accepted human edit, never by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "tests" / "golden.lock.json"
DEFAULT_GOLDEN_DIR = REPO_ROOT / "tests" / "golden"


def load_lock(lock_path: Path) -> dict[str, str]:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AssertionError(f"lock file missing: {lock_path}") from None
    except json.JSONDecodeError as exc:
        raise AssertionError(f"lock file is not valid JSON: {lock_path}: {exc}") from exc
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise AssertionError(f"lock file has no pinned 'files' map: {lock_path}")
    pinned = {}
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise AssertionError(f"lock entry must be str->str: {name!r}")
        pinned[name] = digest
    return pinned


def verify(lock_path: Path, golden_dir: Path) -> list[str]:
    """Check golden_dir against lock_path; return a list of problems (empty = ok)."""
    pinned = load_lock(lock_path)
    if not golden_dir.is_dir():
        return [f"golden directory missing: {golden_dir}"]
    actual = {p.name: p for p in golden_dir.iterdir() if p.is_file()}
    problems: list[str] = []
    for name in sorted(set(pinned) - set(actual)):
        problems.append(f"missing: {name}")
    for name in sorted(set(actual) - set(pinned)):
        problems.append(f"extra: {name}")
    for name in sorted(set(pinned) & set(actual)):
        digest = hashlib.sha256(actual[name].read_bytes()).hexdigest()
        if digest != pinned[name]:
            problems.append(f"modified: {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--golden-dir", type=Path, default=DEFAULT_GOLDEN_DIR)
    args = parser.parse_args(argv)
    try:
        problems = verify(args.lock, args.golden_dir)
    except AssertionError as exc:
        print(f"golden-lock FAILED: {exc}", file=sys.stderr)
        return 1
    if problems:
        for problem in problems:
            print(f"golden-lock FAILED: {problem}", file=sys.stderr)
        return 1
    print(f"golden-lock OK: {len(load_lock(args.lock))} files verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
