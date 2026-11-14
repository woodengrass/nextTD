import sys

from tdnext.core.errors import PluginError

from .navigation import open_header


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m tdnext.plugins.desktop.shortcut HEADER_ID")
    try:
        open_header(sys.argv[1])
    except PluginError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
