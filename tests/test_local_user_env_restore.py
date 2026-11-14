"""User-facing PATH restoration via real child execution boundaries."""

from __future__ import annotations

import pytest
from local_path_fixtures import PY, base_env, run_child


@pytest.mark.parametrize("path_state", ["absent", "empty", "normal"])
def test_user_facing_shell_and_editor_restore_original_path(tmp_path, path_state: str) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    env = base_env(tmp_path, state, path_state=path_state)
    expected = {"absent": None, "empty": "", "normal": "/usr/bin:/bin"}[path_state]
    code = (
        "import sys\n"
        "sys.argv = ['tdnext', 'list']\n"
        "from tdnext.cli import main\n"
        "main()\n"
        "import os\n"
        "from tdnext.core.local_env import restored_env\n"
        "BAD = ('TDNEXT_ORIGINAL_PATH', 'TDNEXT_ORIGINAL_PATH_PRESENT',\n"
        "    'TDNEXT_PATH_BOOTSTRAPPED')\n"
        "restored = restored_env()\n"
        "print('SHELL_PATH=' + repr(restored.get('PATH', '<unset>')))\n"
        "print('MARKERS=' + repr(sorted(k for k in restored if k in BAD)))\n"
        "import subprocess\n"
        "from unittest import mock\n"
        "from pathlib import Path\n"
        "from tdnext.plugins.shell.launch import exec_shell\n"
        "captured = {}\n"
        "def fake_chdir(p): captured['dir'] = str(p)\n"
        "def fake_exec(binary, args, environ): captured['env'] = dict(environ)\n"
        "os.chdir = fake_chdir\n"
        "os.execvpe = fake_exec\n"
        "exec_shell(Path('.'))\n"
        "print('EXEC_PATH=' + repr(captured['env'].get('PATH', '<unset>')))\n"
        "print('EXEC_MARKERS=' + repr(sorted(k for k in captured['env'] if k in BAD)))\n"
        "from tdnext.plugins.files.view import FilesView\n"
        "view = FilesView.__new__(FilesView)\n"
        "sent = {}\n"
        "def fake_run(cmd, **kw):\n"
        "    sent.update(kw)\n"
        "    sent['cmd'] = cmd\n"
        "    class R:\n"
        "        returncode = 0\n"
        "    return R()\n"
        "subprocess.run = fake_run\n"
        "class FakeApp:\n"
        "    def suspend(self):\n"
        "        from contextlib import nullcontext\n"
        "        return nullcontext()\n"
        "    def notify(self, *a, **k): pass\n"
        "with mock.patch.object(FilesView, 'app', new_callable=mock.PropertyMock) as pm:\n"
        "    pm.return_value = FakeApp()\n"
        "    import tempfile\n"
        "    target = Path(tempfile.mkdtemp()) / 'note.txt'\n"
        "    target.write_text('hi')\n"
        "    view.directory = target.parent\n"
        "    view.reload_directory = lambda *a, **k: None\n"
        "    os.environ['EDITOR'] = 'true'\n"
        "    view.edit(target)\n"
        "print('EDITOR_PATH=' + repr(sent.get('env', {}).get('PATH', '<unset>')))\n"
        "print('EDITOR_MKS=' + repr(sorted(k for k in sent.get('env', {}) if k in BAD)))\n"
    )
    result = run_child([PY, "-c", code], env)
    assert result.returncode == 0, result.stderr
    want = "<unset>" if expected is None else expected
    assert f"SHELL_PATH={want!r}" in result.stdout, result.stdout
    assert "MARKERS=[]" in result.stdout, result.stdout
    assert f"EXEC_PATH={want!r}" in result.stdout, result.stdout
    assert "EXEC_MARKERS=[]" in result.stdout, result.stdout
    assert f"EDITOR_PATH={want!r}" in result.stdout, result.stdout
    assert "EDITOR_MKS=[]" in result.stdout, result.stdout


def test_pty_child_shell_observes_original_path(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    env = base_env(tmp_path, state, path_state="empty")
    env["SHELL"] = "/bin/sh"
    code = (
        "import sys\n"
        "sys.argv = ['tdnext', 'list']\n"
        "from tdnext.cli import main\n"
        "main()\n"
        "from tdnext.plugins.shell.transport import PtySession\n"
        "from pathlib import Path\n"
        "import tempfile\n"
        "captured = {}\n"
        "real_popen = __import__('subprocess').Popen\n"
        "def fake_popen(*a, **k):\n"
        "    captured['env'] = dict(k.get('env', {}))\n"
        "    return real_popen(*a, **k)\n"
        "import subprocess\n"
        "subprocess.Popen = fake_popen\n"
        "BAD = ('TDNEXT_ORIGINAL_PATH', 'TDNEXT_ORIGINAL_PATH_PRESENT')\n"
        "BAD = BAD + ('TDNEXT_PATH_BOOTSTRAPPED',)\n"
        "session = PtySession(Path(tempfile.mkdtemp()))\n"
        "print('PTY_ENV_PATH=' + repr(captured['env'].get('PATH', '<unset>')))\n"
        "print('PTY_MKS=' + repr(sorted(k for k in captured['env'] if k in BAD)))\n"
        "import anyio\n"
        "anyio.run(session.aclose)\n"
    )
    result = run_child([PY, "-c", code], env)
    assert result.returncode == 0, result.stderr
    assert "PTY_ENV_PATH=''" in result.stdout, result.stdout
    assert "PTY_MKS=[]" in result.stdout, result.stdout


def test_nested_reentry_is_stable_and_has_no_empty_component(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    env = base_env(tmp_path, state, path_state="normal")
    code = (
        "import sys\n"
        "sys.argv = ['tdnext', 'list']\n"
        "from tdnext.cli import main\n"
        "main()\n"
        "import os\n"
        "from tdnext.core.local_env import bootstrap, restored_env\n"
        "first = os.environ['PATH']\n"
        "os.environ['TDNEXT_STATE_DIR'] = '/tmp/should-not-matter'\n"
        "second = bootstrap()\n"
        "print('PATH=' + os.environ['PATH'])\n"
        "print('FIRST=' + first)\n"
        "print('SAME=' + repr(first == os.environ['PATH']))\n"
        "print('COUNT=' + repr(os.environ['PATH'].split(':').count(str(second))))\n"
        "print('EMPTYCOMP=' + repr('' in os.environ['PATH'].split(':')))\n"
        "print('RESTORED=' + repr(restored_env().get('PATH')))\n"
    )
    result = run_child([PY, "-c", code], env)
    assert result.returncode == 0, result.stderr
    assert "SAME=True" in result.stdout, result.stdout
    assert "EMPTYCOMP=False" in result.stdout, result.stdout
    assert "RESTORED='/usr/bin:/bin'" in result.stdout, result.stdout
