"""Archive-only parity runner for the unified ANSI golden screens.

Every scene launches ONLY the extracted distribution under test, once per
terminal mode in TERMINALS (``linux`` and ``xterm-256color``)::

    <dist>/tdnext desktop <workdir>

There is no direct-config branch, no reference replay, and no runtime
override: views resolve through the archive siblings (``state/bin``, then
the directory beside ``tdnext``, then the controlled ``PATH`` below), and
this module never reads ``TDNEXT_CORE_BINARY``/``TDNEXT_FILES_BINARY``/
``TDNEXT_DESKTOP_BINARY`` or any other alternate launch/config hook.

Read-only frame comparison lives in :mod:`golden_screen`; this module only
drives focused keyboard input, waits on visible readiness text (never exact
ANSI sequences), and then verifies that the operations actually persisted:
renamed/deleted files on disk, the new provider in ``appearance.json``,
and the toggled plugin in ``plugins.json``. The golden frame itself is
captured before any mutating keystroke, so full-cell appearance equality
is preserved without masks.

The distribution under test arrives exclusively through the
``TDNEXT_VERIFIED_DIST_ROOT`` environment variable, set by
``scripts/verify-dist.py`` when it invokes pytest. There is no facility
here for regenerating reference files.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from golden_screen import ANSI16_DIR, wait_for_frame
from native_processes import tagged_processes, terminate_tagged

DIST_ENV = 'TDNEXT_VERIFIED_DIST_ROOT'
WORK_ROOT = Path('/tmp/tdnext-golden-screen')
LOCK_PATH = Path('/tmp/tdnext-golden-screen.lock')
# Provenance pins for the frozen golden set: these identify the reference
# source tree and core binary the locked .screen files were recorded from.
# They are checked against tests/golden.lock.json by tests/test_golden_lock.py
# and are never used to launch or override anything in this runner.
SOURCE_SHA = 'ca7f7e695b4e025058fe8e9e6e22e8b41e4704f1218b06e2753100de22a9a330'
CORE_SHA = 'df1753f9b89e2724451b1b9c066efaed80c610c98278634cfc8f2dc73c4d50b4'
ARCHIVE_BINARIES = ('tdnext', 'tdnext-desktop', 'tdnext-core', 'tdnext-files', 'tdnext-settings')
ARCHIVE_LICENSE = 'LICENSE.zellij-0.45.1'
SCENES = {
    'files_120x38': (120, 38, ()),
    'files_rename_120x38': (120, 38, (('r', 'Rename to'),)),
    'files_delete_120x38': (120, 38, (('d', 'Delete permanently?'),)),
    'settings_general_120x38': (120, 38, (('M-o', 'Provider:'),)),
    'settings_plugins_120x38': (120, 38, (('M-o', 'Provider:'), ('BTab Enter', '> Plugins'))),
    'files_compact_104x28': (104, 28, ()),
    'monitor_compact_104x28': (104, 28, (('M-2', 'Terminal size too small'),)),
    'shell_120x38': (120, 38, (('M-3', 'Shell keys'),)),
    'files_codicons_120x38': (120, 38, ()),
}
TERMINALS = ('linux', 'xterm-256color')
_POLL_INTERVAL = 0.2


def dist_root() -> Path:
    """Resolve the extracted distribution under test, nothing else."""
    raw = os.environ.get(DIST_ENV, '')
    if not raw:
        raise AssertionError(
            f'{DIST_ENV} is not set; run the golden suite through scripts/verify-dist.py'
        )
    root = Path(raw)
    if not root.is_absolute():
        raise AssertionError(f'{DIST_ENV} must be absolute, got {raw!r}')
    problems = []
    for name in (*ARCHIVE_BINARIES, ARCHIVE_LICENSE):
        candidate = root / name
        if name == ARCHIVE_LICENSE:
            if not candidate.is_file() or candidate.stat().st_size == 0:
                problems.append(f'{name} is missing or empty')
        elif not candidate.is_file() or not os.access(candidate, os.X_OK):
            problems.append(f'{name} is missing or not executable')
    if problems:
        raise AssertionError(f'{DIST_ENV}={root} is not a staged distribution: ' + '; '.join(problems))
    return root


def child_env(home: Path, state: Path, tag: str, term: str) -> dict[str, str]:
    """Controlled child environment: no Python/source paths, no hooks.

    The desktop inherits exactly this map (the shell pane restores it), so
    Python interpreters, source checkouts, and alternate launch/config
    hooks cannot leak into any pane. PATH is explicit and minimal: the
    archive siblings resolve beside the running `tdnext`, `btop` and `sh`
    come from the system directories, and nothing else is reachable.
    TERM is the requested terminal mode; the pane process itself sees it
    through the `env TERM=...` execution-boundary wrapper in desktop(),
    because tmux otherwise reports its own default-terminal instead.
    """
    if term not in TERMINALS:
        raise AssertionError(f'unknown terminal mode: {term!r}; expected one of {TERMINALS!r}')
    env = {key: value for key, value in os.environ.items() if not key.startswith('ZELLIJ')}
    env.update({
        'HOME': str(home),
        'TDNEXT_STATE_DIR': str(state),
        'TDNEXT_TEST_SESSION': tag,
        'TERM': term,
        'LC_ALL': 'C.UTF-8',
        'TZ': 'UTC',
        'SHELL': '/bin/sh',
        'PS1': '$ ',
        'COLORTERM': '',
        'PYTHONDONTWRITEBYTECODE': '1',
        'XDG_CONFIG_HOME': str(home / 'config'),
        'XDG_CACHE_HOME': str(home / 'cache'),
        'XDG_DATA_HOME': str(home / 'data'),
        'XDG_STATE_HOME': str(home / 'xdg-state'),
        'XDG_RUNTIME_DIR': str(home / 'runtime'),
    })
    for key in (
        'BASH_ENV', 'ENV', 'CDPATH', 'NO_COLOR', 'FORCE_COLOR', 'CLICOLOR', 'CLICOLOR_FORCE',
        'PYTHONPATH', 'PYTHONHOME', 'PYTHONSAFEPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX',
        'TDNEXT_CORE_BINARY', 'TDNEXT_FILES_BINARY', 'TDNEXT_DESKTOP_BINARY',
        'TDNEXT_ZELLIJ_SRC', 'TDNEXT_TEST_PATCHED_CORE',
    ):
        env.pop(key, None)
    for key in [key for key in env if key.startswith(('UV_', 'CONDA_', 'VIRTUALENV_'))]:
        env.pop(key, None)
    env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
    return env


def tmux(socket: Path, *args: str) -> str:
    return subprocess.run(['tmux', '-S', str(socket), *args], check=True, capture_output=True,
                          text=True, encoding='utf-8', timeout=10).stdout


def wait_text(socket: Path, text: str, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    screen = ''
    while time.monotonic() < deadline:
        screen = tmux(socket, 'capture-pane', '-p', '-t', 'golden')
        if text.lower() in screen.lower():
            return screen
    raise AssertionError(f'timed out waiting for {text!r}:\n{screen}')


def wait_plain(socket: Path, *texts: str, timeout: float = 30) -> str:
    """Wait until every text is visible in the plain (style-free) capture."""
    deadline = time.monotonic() + timeout
    screen = ''
    while time.monotonic() < deadline:
        screen = tmux(socket, 'capture-pane', '-p', '-t', 'golden')
        if all(text.lower() in screen.lower() for text in texts):
            return screen
    raise AssertionError(f'timed out waiting for {texts!r}:\n{screen}')


def poll_state(timeout: float, description: str, check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return None
        if time.monotonic() >= deadline:
            raise AssertionError(f'timed out waiting for persisted state: {description}')
        time.sleep(_POLL_INTERVAL)


def verify_dist_panes(tag: str, dist: Path) -> None:
    """Prove the panes run archive executables, not text or Python.

    Every tagged process must be free of Python interpreters, source-tree
    paths, and test-override variables, and the real archived Files pane
    binary must be observed among them.
    """
    files_binary = dist / 'tdnext-files'
    saw_files = False
    for pid, entries, command in tagged_processes(tag):
        if not command or not command[0]:
            continue
        program = os.fsdecode(command[0])
        lowered = program.lower()
        if 'python' in lowered or '.venv' in program or '/src/tdnext' in program:
            raise AssertionError(f'archive desktop must not run Python: pid={pid} {command!r}')
        process_env = dict(
            os.fsdecode(entry).split('=', 1) for entry in entries if b'=' in entry
        )
        for banned in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
            if banned in process_env:
                raise AssertionError(f'archive pane leaks {banned}: pid={pid} {command!r}')
        try:
            if os.path.samefile(f'/proc/{pid}/exe', files_binary):
                saw_files = True
        except OSError:
            continue
    if not saw_files:
        raise AssertionError(
            f'archived tdnext-files pane not observed for tag {tag!r}; '
            'the scene must run the archive executables, not just their text'
        )


@contextmanager
def desktop(name: str, term: str):
    cols, rows, _ = SCENES[name]
    if term not in TERMINALS:
        raise AssertionError(f'unknown terminal mode: {term!r}; expected one of {TERMINALS!r}')
    dist = dist_root()
    # A fixed visible path keeps all file-browser cells reproducible. A lock
    # serializes independent pytest processes without touching existing data.
    with open(LOCK_PATH, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        WORK_ROOT.mkdir(mode=0o700)
        try:
            work = WORK_ROOT / 'work'
            work.mkdir()
            example = work / 'example.txt'
            example.write_text('golden example\n', encoding='utf-8')
            os.utime(example, (946684800, 946684800))
            with tempfile.TemporaryDirectory(prefix='tdnext-golden-') as tmp:
                home = Path(tmp)
                (home / 'runtime').mkdir(mode=0o700)
                state = home / 'state'
                (state / 'bin').mkdir(parents=True)
                ids = ['desktop', 'files', 'icons', 'monitor', 'settings', 'shell']
                (state / 'plugins.json').write_text(
                    json.dumps({'installed': ids, 'enabled': ids}), encoding='utf-8')
                if name.startswith('files_codicons_'):
                    (state / 'appearance.json').write_text(
                        json.dumps({'provider_id': 'icons', 'icon_mode': 'codicons'}),
                        encoding='utf-8',
                    )
                socket = home / 'tmux.sock'
                tag = f'golden-{term}-' + Path(tmp).name
                env = child_env(home, state, tag, term)
                # The only supported entry point: the extracted manager builds
                # the session config from enabled plugins and launches the
                # sibling desktop. No --config, no direct binary launch.
                # TERM reaches the pane process via this execution-boundary
                # env wrapper (-f /dev/null leaves no user config; the tmux
                # server env alone would still report default-terminal).
                command = ['env', f'TERM={term}', 'COLORTERM=',
                           str(dist / 'tdnext'), 'desktop', str(work)]
                try:
                    subprocess.run(['tmux', '-S', str(socket), '-f', '/dev/null', 'new-session',
                                    '-d', '-s', 'golden', '-x', str(cols), '-y', str(rows),
                                    *command], env=env, check=True, capture_output=True, timeout=10)
                    yield socket, tag, work, state
                finally:
                    subprocess.run(['tmux', '-S', str(socket), 'kill-server'], env=env,
                                   capture_output=True, check=False, timeout=10)
                    terminate_tagged(tag)
        finally:
            shutil.rmtree(WORK_ROOT)


def confirm_rename(socket: Path, work: Path) -> None:
    """Complete the rename dialog and prove the real file moved."""
    tmux(socket, 'send-keys', '-t', 'golden', 'renamed.txt')
    tmux(socket, 'send-keys', '-t', 'golden', 'Enter')

    def renamed() -> bool:
        return (work / 'renamed.txt').is_file() and not (work / 'example.txt').exists()

    poll_state(30, 'rename persisted on disk', renamed)
    wait_text(socket, 'renamed.txt')


def confirm_delete(socket: Path, work: Path) -> None:
    """Confirm the delete dialog and prove the real file is gone."""

    def deleted() -> bool:
        return not (work / 'example.txt').exists()

    tmux(socket, 'send-keys', '-t', 'golden', 'Tab')
    tmux(socket, 'send-keys', '-t', 'golden', 'Space')
    poll_state(30, 'delete persisted on disk', deleted)


def choose_codicons_provider(socket: Path, state: Path) -> None:
    """Pick the Codicons provider in Settings and prove it persisted."""
    tmux(socket, 'send-keys', '-t', 'golden', 'Enter')
    tmux(socket, 'send-keys', '-t', 'golden', 'Down')
    tmux(socket, 'send-keys', '-t', 'golden', 'Enter')
    wait_text(socket, 'Appearance saved.')

    def provider_saved() -> bool:
        try:
            appearance = json.loads((state / 'appearance.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return appearance.get('provider_id') == 'icons'

    poll_state(30, 'appearance.json carries the icons provider', provider_saved)


def disable_codicons_plugin(socket: Path, state: Path) -> None:
    """Disable the Codicons plugin in Settings and prove it persisted."""
    tmux(socket, 'send-keys', '-t', 'golden', 'Down')
    tmux(socket, 'send-keys', '-t', 'golden', 'Down')
    tmux(socket, 'send-keys', '-t', 'golden', 'd')
    wait_text(socket, 'Restart desktop to apply plugin changes.')

    def plugin_disabled() -> bool:
        try:
            plugins = json.loads((state / 'plugins.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return 'icons' not in plugins.get('enabled', [])

    poll_state(30, 'plugins.json no longer enables icons', plugin_disabled)


def scene(name: str, term: str) -> str:
    cols, rows, actions = SCENES[name]
    if term not in TERMINALS:
        raise AssertionError(f'unknown terminal mode: {term!r}; expected one of {TERMINALS!r}')
    with desktop(name, term) as (socket, tag, work, state):
        # Wait for the prompt before every input: the Files view is the
        # initial view in all scenes, so its readiness gates all keys.
        wait_text(socket, 'example.txt')
        wait_text(socket, 'PREVIEW')
        wait_text(socket, 'Modified 2000-01-01')
        if cols == 120:
            wait_text(socket, 'Size')
        if name.startswith('files_codicons_'):
            for glyph, label in (('\uea83', 'Files'), ('\ueb31', 'Monitor'), ('\uea85', 'Shell')):
                screen = wait_text(socket, glyph)
                assert any(glyph in rail and label in rail for rail, _, _ in (
                    line.partition('│') for line in screen.splitlines()
                )), f'{label} Codicon is not displayed in the rail:\n{screen}'
        for key, expected in actions:
            tmux(socket, 'send-keys', '-t', 'golden', *key.split())
            wait_text(socket, expected)
            if key == 'M-o':
                # Readiness is semantic (visible provider text), never an
                # exact ANSI sequence; real focus is proven by the Plugins
                # navigation and the persisted choice below.
                wait_plain(socket, 'Provider:', 'None (text)')
        if name.startswith('shell_'):
            wait_text(socket, '$')
            tmux(socket, 'send-keys', '-t', 'golden', "printf 'GOLDEN_RESULT:%s\\n' hello", 'Enter')
            wait_text(socket, 'GOLDEN_RESULT:hello')
        verify_dist_panes(tag, dist_root())
        frame = wait_for_frame(name, socket, 'golden', cols, rows, golden_dir=ANSI16_DIR)
        # The golden frame above is the frozen contract. Only now, with
        # equality already proven, complete the operation the dialog offered
        # and verify it persisted through the real archive panes.
        if name == 'files_rename_120x38':
            confirm_rename(socket, work)
        elif name == 'files_delete_120x38':
            confirm_delete(socket, work)
        elif name == 'settings_general_120x38':
            choose_codicons_provider(socket, state)
        elif name == 'settings_plugins_120x38':
            disable_codicons_plugin(socket, state)
        return frame
