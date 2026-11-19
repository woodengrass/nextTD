"""Attach a subprocess session to its slave PTY before executing the shell."""

import fcntl
import os
import sys
import termios


def main() -> None:
    shell = sys.argv[1]
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.environ.pop("TDNEXT_PLUGIN_CODE", None)
        os.execvpe(shell, [shell, "-i"], os.environ)
    except OSError as exc:
        print(f"Cannot start shell {shell}: {exc}", file=sys.stderr)
        raise SystemExit(127) from exc


if __name__ == "__main__":
    main()
