#!/usr/bin/env python3
"""Verify one immutable native distribution archive end to end.

Usage:
    python3 scripts/verify-dist.py ARCHIVE.tar.gz

The verifier never builds anything; it only checks and runs:

1. Frozen locks (mandatory, both byte-guards): ``scripts/check-golden-lock.py``
   over the archival ``tests/golden/*.screen`` set pinned by
   ``tests/golden.lock.json``, plus the same checker with explicit
   ``--lock/--golden-dir`` over the unified ANSI oracle
   ``tests/golden/ansi16/*.screen`` pinned by
   ``tests/golden.lock.ansi16.json``. The archival nine stay untouched;
   the ANSI set is the active full-styled-cell oracle.
2. Safe exact tar layout: the archive must contain exactly
   ``tdnext/`` with the five real executable binaries plus the license,
   and nothing else (no absolute paths, no ``..``, no links, one
   directory root).
3. Read the archive bytes once: the same private bytes are hashed,
   layout-checked, and extracted. The tree is extracted once to a fresh
   directory outside the checkout, removed on every exit.
4. Probe the archived pane executables themselves: each must be a static
   ELF executable (no program interpreter, so it runs on any Linux x86-64
   glibc or musl system), then core chrome version, desktop help, manager
   list -- executables, not text.
5. Run the existing functional smoke (``scripts/smoke-dist.sh``) against
   the extracted tree.
6. Run every named scene in each terminal mode (``linux``,
   ``xterm-256color``) through ONLY the extracted ``tdnext desktop``,
   with TERM injected at the program execution boundary via
   ``env TERM=<mode>`` (the tmux server env alone still reports its own
   default-terminal), empty COLORTERM, and a controlled child
   environment (no Python/source paths, no alternate launch/config
   hooks, no pytest option injection). A machine-readable junit report
   must show every scene-mode pair from the ANSI lock passing exactly
   once; skips, filters, and deselections fail the gate.

On success the archive SHA-256 digest is printed and, if
TDNEXT_VERIFIED_DIGEST_OUT is set, written there as a single hex line. Any failure exits
non-zero with the failing step named.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / 'tests' / 'golden'
LOCK_FILE = ROOT / 'tests' / 'golden.lock.json'
ANSI16_DIR = GOLDEN_DIR / 'ansi16'
ANSI16_LOCK = ROOT / 'tests' / 'golden.lock.ansi16.json'
LOCK_SCRIPT = ROOT / 'scripts' / 'check-golden-lock.py'
SMOKE_SCRIPT = ROOT / 'scripts' / 'smoke-dist.sh'
GOLDEN_TEST = 'tests/test_desktop_golden.py'
GOLDEN_CASE = 'test_desktop_matches_frozen_golden_through_archive'
TERMINALS = ('linux', 'xterm-256color')
TOP = 'tdnext'
EXPECTED_MEMBERS = (
    'tdnext',
    'tdnext-desktop',
    'tdnext-core',
    'tdnext-files',
    'tdnext-settings',
    'LICENSE.zellij-0.45.1',
)
BINARIES = EXPECTED_MEMBERS[:5]


def fail(step: str, message: str) -> NoReturn:
    print(f'verify-dist: error in {step}: {message}', file=sys.stderr)
    raise SystemExit(1)


def check_frozen_lock() -> None:
    step = 'frozen lock'
    if not LOCK_SCRIPT.is_file():
        fail(step, f'{LOCK_SCRIPT} is missing; the lock check is mandatory, never skipped')
    for args in ([], ['--lock', str(ANSI16_LOCK), '--golden-dir', str(ANSI16_DIR)]):
        result = subprocess.run(
            [sys.executable, str(LOCK_SCRIPT), *args], cwd=ROOT,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            label = 'archival lock' if not args else 'ansi16 lock'
            fail(step, f'{LOCK_SCRIPT.name} rejected the tree ({label}):\n{result.stdout}{result.stderr}')


def lock_screens() -> list[str]:
    """Scene names pinned by the active ANSI lock, not a local constant."""
    step = 'ansi16 lock'
    try:
        files = json.loads(ANSI16_LOCK.read_text(encoding='utf-8'))['files']
    except (OSError, ValueError, KeyError) as exc:
        fail(step, f'cannot read pinned scene set from {ANSI16_LOCK}: {exc}')
    if not isinstance(files, dict) or not files:
        fail(step, f'no pinned scenes in {ANSI16_LOCK}')
    return sorted(Path(name).stem for name in files)


def read_archive(archive: Path) -> bytes:
    step = 'tar layout'
    if not archive.is_file():
        fail(step, f'archive not found: {archive}')
    if archive.suffixes[-2:] != ['.tar', '.gz'] and archive.suffix != '.tgz':
        fail(step, f'archive must be a .tar.gz file: {archive}')
    try:
        return archive.read_bytes()
    except OSError as exc:
        fail(step, f'cannot read archive: {exc}')


def check_layout(data: bytes) -> None:
    step = 'tar layout'
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            members = tar.getmembers()
    except (tarfile.TarError, OSError) as exc:
        fail(step, f'cannot read archive: {exc}')
    seen: set[str] = set()
    roots = 0
    for member in members:
        name = member.name
        if name == TOP or name == f'{TOP}/':
            roots += 1
            if not member.isdir():
                fail(step, f'archive root {name!r} is not a directory')
            continue
        if not name.startswith(f'{TOP}/'):
            fail(step, f'member outside {TOP}/: {name!r}')
        if member.name in seen:
            fail(step, f'duplicate member: {name!r}')
        seen.add(member.name)
        if member.issym() or member.islnk():
            fail(step, f'link member not allowed: {name!r}')
        if not member.isfile():
            fail(step, f'non-file member not allowed: {name!r}')
        short = name[len(TOP) + 1:]
        if '/' in short or short not in EXPECTED_MEMBERS:
            fail(step, f'unexpected member: {name!r}')
    if roots != 1:
        fail(step, f'archive must hold exactly one {TOP}/ directory root, found {roots}')
    wanted = {f'{TOP}/{name}' for name in EXPECTED_MEMBERS}
    if seen != wanted:
        fail(step, f'exact layout mismatch: missing={sorted(wanted - seen)} extra={sorted(seen - wanted)}')


def extract_once(data: bytes, dest: Path) -> Path:
    step = 'extract'
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            tar.extractall(dest, filter='data')
    except (tarfile.TarError, OSError) as exc:
        fail(step, f'cannot extract archive: {exc}')
    return dest / TOP


def elf_interpreter(path: Path) -> str | None:
    """Return the PT_INTERP path of an ELF64 little-endian executable, or None."""
    step = 'static executables'
    try:
        data = path.read_bytes()
    except OSError as exc:
        fail(step, f'cannot read {path.name}: {exc}')
    if data[:4] != b'\x7fELF' or data[4:6] != b'\x02\x01':
        fail(step, f'{path.name} is not an ELF64 little-endian executable')
    try:
        phoff, = struct.unpack_from('<Q', data, 0x20)
        phentsize, phnum = struct.unpack_from('<HH', data, 0x36)
        for index in range(phnum):
            header = phoff + index * phentsize
            p_type, = struct.unpack_from('<I', data, header)
            if p_type == 3:  # PT_INTERP
                offset, = struct.unpack_from('<Q', data, header + 8)
                size, = struct.unpack_from('<Q', data, header + 32)
                return data[offset:offset + size].rstrip(b'\0').decode(errors='replace')
    except struct.error:
        fail(step, f'{path.name} has a truncated ELF header')
    return None


def scrub_env() -> dict[str, str]:
    """Verifier environment: no nesting, no test overrides, no hooks."""
    env = dict(os.environ)
    env.pop('ZELLIJ', None)
    env.pop('ZELLIJ_SESSION_NAME', None)
    for key in (
        'TDNEXT_CORE_BINARY', 'TDNEXT_FILES_BINARY', 'TDNEXT_DESKTOP_BINARY',
        'TDNEXT_ZELLIJ_SRC', 'TDNEXT_TEST_PATCHED_CORE',
    ):
        env.pop(key, None)
    return env


def run_logged(step: str, command: list[str], env: dict[str, str]) -> None:
    print(f'verify-dist: [{step}] {" ".join(command)}', flush=True)
    result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                            stdout=sys.stdout, stderr=sys.stderr, timeout=1800)
    if result.returncode != 0:
        fail(step, f'command exited with status {result.returncode}')


def probe_executables(dist: Path, env: dict[str, str]) -> None:
    step = 'probe executables'
    for name in BINARIES:
        candidate = dist / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            fail(step, f'{name} is missing or not executable after extraction')
        interpreter = elf_interpreter(candidate)
        if interpreter is not None:
            fail('static executables',
                 f'{name} is dynamically linked (interpreter {interpreter}); '
                 'release executables must be static')
    if list(dist.glob('*.whl')):
        fail(step, 'wheel present in native distribution')
    if (dist / 'LICENSE.zellij-0.45.1').stat().st_size == 0:
        fail(step, 'license is empty')
    core = subprocess.run([str(dist / 'tdnext-core'), '--tdnext-chrome-version'],
                          env=env, capture_output=True, text=True, timeout=30)
    if core.returncode != 0 or core.stdout.strip() != '1':
        fail(step, f'tdnext-core is not native chrome version 1: rc={core.returncode} out={core.stdout!r}')
    desktop = subprocess.run([str(dist / 'tdnext-desktop'), '--help'],
                             env=env, capture_output=True, text=True, timeout=30)
    if desktop.returncode != 0:
        fail(step, f'tdnext-desktop --help failed: rc={desktop.returncode}')
    home = Path(tempfile.mkdtemp(prefix='tdnext-verify-probe-'))
    probe_env = dict(env, HOME=str(home), TDNEXT_STATE_DIR=str(home / 'state'))
    try:
        listing = subprocess.run([str(dist / 'tdnext'), 'list'],
                                 env=probe_env, capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if listing.returncode != 0 or 'files' not in listing.stdout:
        fail(step, f'tdnext list failed: rc={listing.returncode} out={listing.stdout!r}')


def run_golden_scenes(dist: Path, env: dict[str, str], expected: list[str], workdir: Path) -> None:
    step = 'archive golden scenes'
    pytest = ROOT / '.venv' / 'bin' / 'pytest'
    if pytest.is_file() and os.access(pytest, os.X_OK):
        pytest_command = [str(pytest)]
    else:
        pytest_command = [sys.executable, '-m', 'pytest']
    report = workdir / 'golden-junit.xml'
    golden_env = dict(env, TDNEXT_VERIFIED_DIST_ROOT=str(dist))
    golden_env.pop('TDNEXT_GOLDEN_VIA_VERIFY_DIST', None)
    golden_env.pop('TDNEXT_DESELECTED_REPORT', None)
    for key in [key for key in golden_env if key.startswith('PYTEST_')]:
        golden_env.pop(key)
    run_logged(step, [*pytest_command, GOLDEN_TEST, '-q', f'--junitxml={report}'], golden_env)
    expected_ids = [f'{GOLDEN_CASE}[{scene}-{term}]' for scene in expected for term in TERMINALS]
    try:
        tree = ET.parse(report)
    except (OSError, ET.ParseError) as exc:
        fail(step, f'cannot read junit report {report}: {exc}')
    observed: Counter[str] = Counter()
    bad: set[str] = set()
    for case in tree.getroot().iter('testcase'):
        # junit `name` holds the parametrized ID; `classname` varies with layout.
        test_id = str(case.get('name'))
        observed[test_id] += 1
        if (
            case.find('failure') is not None
            or case.find('error') is not None
            or case.find('skipped') is not None
        ):
            bad.add(test_id)
    rerun = {test_id for test_id, count in observed.items() if count != 1}
    if rerun:
        fail(step, f'scene ran more than once or was filtered: {sorted(rerun)}')
    if set(observed) != set(expected_ids):
        fail(step, f'scene set mismatch: missing={sorted(set(expected_ids) - set(observed))} '
                    f'extra={sorted(set(observed) - set(expected_ids))}')
    if bad:
        fail(step, f'scene did not pass cleanly: {sorted(bad)}')


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    archive = Path(argv[1])
    if not archive.is_absolute():
        archive = Path.cwd() / archive
    check_frozen_lock()
    expected = lock_screens()
    data = read_archive(archive)
    digest = hashlib.sha256(data).hexdigest()
    digest_out = os.environ.pop('TDNEXT_VERIFIED_DIGEST_OUT', None)
    check_layout(data)
    dest = Path(tempfile.mkdtemp(prefix='tdnext-verify-'))
    try:
        dist = extract_once(data, dest)
        print(f'verify-dist: extracted {archive} to {dist}', flush=True)
        env = scrub_env()
        probe_executables(dist, env)
        run_logged('functional smoke', ['bash', str(SMOKE_SCRIPT), str(dist)], env)
        run_golden_scenes(dist, env, expected, dest)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    if digest_out:
        Path(digest_out).write_text(digest + '\n', encoding='ascii')
    print(f'verify-dist: ok sha256={digest} archive={archive}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
