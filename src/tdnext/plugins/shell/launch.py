"""Resolve and execute the user's interactive shell without a command string."""

import os
import pwd
from pathlib import Path

from tdnext.core.local_env import restored_env


def user_shell() -> str:
    return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def exec_shell(directory: Path) -> None:
    shell = user_shell()
    os.chdir(directory)
    os.execvpe(shell, [shell, "-i"], restored_env())
