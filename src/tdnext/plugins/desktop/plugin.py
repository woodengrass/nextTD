"""Desktop shell plugin entry point."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.local_env import restored_env
from tdnext.core.models import PluginManifest
from tdnext.core.session import SESSION_ENV, SessionSnapshot, session_token
from tdnext.runtime import state_dir

from .app import DesktopApp
from .contract import textual_views
from .navigation import BROKER_ENABLE_ENV, BROKER_HEADER_ENV, CORE_ENABLE_ENV, CORE_VERSION_ENV, tab_name

BROKER_WASM_NAME = "navigator-broker.wasm"

CORE_BINARY_NAME = "zellij-core-navigation-v3"
CORE_REQUIRED_COMMANDS = (
    "open-tab-with-return", "open-tab-with-return-from-header", "restore-focus-from-header", "return-to-origin",
    "set-pane-fixed-width",
)
CORE_PROBE_TIMEOUT = 5

BROKER_PERMISSIONS = (
    "ReadApplicationState",
    "ChangeApplicationState",
    "Reconfigure",
    "ReadCliPipes",
)


def broker_asset_path() -> Path | None:
    """Return the bundled broker WASM path when it is a usable artifact.

    The file must exist next to this module and carry the WASM magic
    (``\\0asm``). Anything else (missing, empty, wrong magic) means the
    native session falls back to the original Python shortcut path.
    """
    candidate = Path(__file__).with_name(BROKER_WASM_NAME)
    try:
        with candidate.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return None
    if magic != b"\0asm":
        return None
    return candidate


class BrokerSnapshot(NamedTuple):
    """One atomic startup snapshot of the broker asset and its cached grants.

    Taken once per native launch so the session env flag and the layout
    ``active`` flag always agree, even if the asset or permission cache
    changes mid-launch.
    """

    wasm: Path | None
    active: bool


_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _skip_nested_block(text: str, pos: int) -> tuple[int | None, bool]:
    """Skip a ``/* ... */`` comment starting at ``pos`` (block comments nest).

    Returns the position after the closing delimiter and whether the comment
    spanned lines, or ``(None, ...)`` when the comment is unterminated.
    """
    depth = 0
    index = pos
    spanned = False
    while index < len(text):
        if text.startswith("/*", index):
            depth += 1
            index += 2
        elif text.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index, spanned
        else:
            if text[index] == "\n":
                spanned = True
            index += 1
    return None, spanned


def _skip_inline(text: str, pos: int) -> tuple[int, bool] | None:
    """Skip same-line trivia (spaces and comments) after a node name.

    Returns ``(new_pos, crossed_newline)`` or ``None`` on an unterminated
    block comment. A ``//`` comment consumes through its newline.
    """
    crossed = False
    while pos < len(text):
        char = text[pos]
        if char in (" ", "\t", "\r"):
            pos += 1
        elif text.startswith("//", pos):
            end = text.find("\n", pos)
            if end < 0:
                return len(text), True
            pos = end + 1
            crossed = True
        elif text.startswith("/*", pos):
            end_pos, spanned = _skip_nested_block(text, pos)
            if end_pos is None:
                return None
            pos = end_pos
            crossed = crossed or spanned
        else:
            break
    return pos, crossed


def _skip_trivia(text: str, pos: int) -> int | None:
    """Skip whitespace, newlines, ``;`` separators, and comments.

    Returns the new position or ``None`` on an unterminated block comment.
    """
    while pos < len(text):
        char = text[pos]
        if char in (" ", "\t", "\r", "\n", ";"):
            pos += 1
        elif text.startswith("//", pos):
            end = text.find("\n", pos)
            if end < 0:
                return len(text)
            pos = end + 1
        elif text.startswith("/*", pos):
            end_pos, _ = _skip_nested_block(text, pos)
            if end_pos is None:
                return None
            pos = end_pos
        else:
            break
    return pos


def _parse_kdl_string(text: str, pos: int) -> tuple[str, int] | None:
    """Parse a double-quoted KDL string starting at ``pos``.

    Returns ``(decoded_value, end_pos)`` or ``None`` when the string uses
    anything outside the strict subset (escapes other than the KDL
    single-line set, raw newlines, unterminated quotes).
    """
    if pos >= len(text) or text[pos] != '"':
        return None
    index = pos + 1
    chars: list[str] = []
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(chars), index + 1
        if char == "\\":
            if index + 1 >= len(text):
                return None
            esc = text[index + 1]
            if esc in _SIMPLE_ESCAPES:
                chars.append(_SIMPLE_ESCAPES[esc])
                index += 2
            elif esc == "u" and text.startswith("u{", index + 1):
                close = text.find("}", index + 3)
                if close < 0 or not 1 <= close - (index + 3) <= 6:
                    return None
                digits = text[index + 3 : close]
                if any(digit not in "0123456789abcdefABCDEF" for digit in digits):
                    return None
                try:
                    code = int(digits, 16)
                except ValueError:
                    return None
                if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                    return None
                chars.append(chr(code))
                index = close + 1
            else:
                return None
        elif char == "\n" or (ord(char) < 0x20 and char != "\t"):
            return None
        else:
            chars.append(char)
            index += 1
    return None


def _parse_ident(text: str, pos: int) -> tuple[str, int] | None:
    """Parse a bare KDL identifier (ASCII letters/digits plus ``_.``-``-``)."""
    if pos >= len(text) or not (text[pos].isascii() and (text[pos].isalpha() or text[pos] == "_")):
        return None
    index = pos + 1
    while index < len(text) and text[index].isascii() and (text[index].isalnum() or text[index] in "_.-"):
        index += 1
    return text[pos:index], index


def _parse_node_name(text: str, pos: int) -> tuple[str, int] | None:
    """Parse a top-level or child node name (quoted string or bare identifier)."""
    if pos < len(text) and text[pos] == '"':
        return _parse_kdl_string(text, pos)
    return _parse_ident(text, pos)


def _parse_children(text: str, pos: int) -> tuple[list[str], int] | None:
    """Parse a ``{ ... }`` children block starting at ``pos``.

    Returns ``(direct_child_names, end_pos)`` or ``None`` when the block uses
    anything outside the strict subset: nested grandchildren, child
    arguments/properties, or an unclosed brace.
    """
    assert text[pos] == "{"
    pos += 1
    kids: list[str] = []
    while True:
        skipped = _skip_trivia(text, pos)
        if skipped is None or skipped >= len(text):
            return None
        pos = skipped
        if text[pos] == "}":
            return kids, pos + 1
        parsed = _parse_node_name(text, pos)
        if parsed is None:
            return None
        name, pos = parsed
        inline = _skip_inline(text, pos)
        if inline is None or inline[0] >= len(text):
            return None
        pos, _ = inline
        if text[pos] == "{":
            return None
        if text[pos] not in ("\n", ";", "}"):
            return None
        kids.append(name)


def _parse_permission_cache(text: str) -> dict[str, list[str]] | None:
    """Parse the strict host-generated subset of ``permissions.kdl``.

    Mirrors ``PermissionCache::from_string`` (zellij-utils ``kdl/mod.rs``):
    only exact top-level path keys count, only direct child node names count
    as permissions, and a later duplicate entry overwrites an earlier one.
    Returns ``None`` when the document uses anything outside the strict
    subset (comments are skipped like the host, but nested keys, child
    arguments, stray text, or an unclosed brace fail the whole document), so
    the caller fails closed toward legacy.
    """
    entries: dict[str, list[str]] = {}
    pos = _skip_trivia(text, 0)
    if pos is None:
        return None
    while pos < len(text):
        parsed = _parse_node_name(text, pos)
        if parsed is None:
            return None
        name, pos = parsed
        inline = _skip_inline(text, pos)
        if inline is None:
            return None
        pos, crossed = inline
        if pos < len(text) and text[pos] == "{" and not crossed:
            children = _parse_children(text, pos)
            if children is None:
                return None
            kids, pos = children
            entries[name] = kids
            inline = _skip_inline(text, pos)
            if inline is None:
                return None
            pos, crossed = inline
            if not crossed and pos < len(text) and text[pos] not in ("\n", ";"):
                return None
        elif pos >= len(text) or text[pos] in ("\n", ";"):
            # Childless node: the host ignores nodes without children.
            pass
        else:
            # Node arguments/properties, a brace on a later line, or stray
            # text: outside the strict subset.
            return None
        pos = _skip_trivia(text, pos)
        if pos is None:
            return None
    return entries


def broker_grants_cached(wasm: Path) -> bool:
    """Whether the user already granted every broker permission.

    Reads the same permission cache the Zellij server reads for the new
    session (`<state>/zellij-cache/zellij/permissions.kdl`). Only a prior
    explicit grant warms the broker: a fresh, denied, or partial cache
    means the session stays on the legacy route and sends no broker pipes,
    because ungranted pipe events are cached by the host instead of
    answered. Any read or parse doubt fails closed toward legacy.
    """
    try:
        text = (state_dir() / "zellij-cache" / "zellij" / "permissions.kdl").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    entries = _parse_permission_cache(text)
    if entries is None:
        return False
    # Zellij 0.45.1 keys this cache with RunPluginLocation's Display (bare path).
    granted = entries.get(str(wasm))
    return granted is not None and all(permission in granted for permission in BROKER_PERMISSIONS)


def broker_snapshot(header_id: str | None) -> BrokerSnapshot:
    """Take one atomic asset+grant snapshot for a native launch."""
    asset = broker_asset_path() if header_id is not None else None
    return BrokerSnapshot(wasm=asset, active=asset is not None and broker_grants_cached(asset))


def broker_ready(header_id: str | None) -> Path | None:
    """Return the broker WASM path only when the session may use it.

    Readiness is proven, never assumed: a valid asset plus cached prior
    grants for this exact path. Otherwise the launcher preloads the pane
    (so the prompt is reachable) but leaves the broker env off, and the
    session runs the legacy route with no broker pipes.
    """
    snapshot = broker_snapshot(header_id)
    return snapshot.wasm if snapshot.active else None


def core_candidate_path() -> Path:
    """Project-local core navigation binary selected only when capable."""
    return state_dir() / "bin" / CORE_BINARY_NAME


def core_capable_binary() -> Path | None:
    """Return the core binary only when it offers the required commands.

    The candidate must be an executable file and ``<binary> action --help``
    (bounded timeout) must list all required navigation commands. Anything
    else (missing, non-executable, probe
    failure, nonzero exit, partial listing) keeps the existing stock or
    experimental broker route.
    """
    packaged = Path(__file__).resolve().parents[2] / "_native" / CORE_BINARY_NAME
    for candidate in (core_candidate_path(), packaged):
        try:
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            result = subprocess.run(
                [str(candidate), "action", "--help"],
                capture_output=True, text=True, check=False, timeout=CORE_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            continue
        if result.returncode == 0:
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            if set(CORE_REQUIRED_COMMANDS).issubset(output.split()):
                return candidate
    return None


def broker_host_compatible(zellij: str) -> bool:
    """Only load the pinned broker into the matching Zellij host."""
    try:
        result = subprocess.run(
            [zellij, "--version"], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "zellij 0.45.1"


def primary_header_id(views: Sequence[tuple[PluginManifest, ViewPlugin]]) -> str | None:
    """Return the first header-placed plugin id, or None when absent."""
    return next((manifest.id for manifest, _ in views if manifest.placement == "header"), None)


def layout_string(
    views: Sequence[tuple[PluginManifest, ViewPlugin]],
    session: SessionSnapshot | None = None,
    broker: BrokerSnapshot | None = None,
) -> str:
    python = json.dumps(sys.executable)
    if not views:
        return (
            f'layout {{\n    tab name="Desktop" focus=true {{\n'
            f"        pane command={python} {{\n"
            '            args "-m" "tdnext.plugins.desktop.panes" "empty"\n'
            "        }\n    }\n}"
        )
    snapshot = f' "--snapshot" {json.dumps(session_token(session))}' if session is not None else ""
    header_id = primary_header_id(views)
    if broker is None:
        broker = broker_snapshot(header_id)
    broker_wasm = broker.wasm if header_id is not None else None
    tabs: list[str] = []
    ordered = [view for view in views if view[0].placement == "sidebar"] + [
        view for view in views if view[0].placement == "header"
    ]
    for index, (manifest, _) in enumerate(ordered):
        name = json.dumps(tab_name(manifest.id) if manifest.placement == "header" else manifest.name)
        plugin_id = json.dumps(manifest.id)
        focused = " focus=true" if index == 0 else ""
        module = "tdnext.plugins.desktop.panes" if manifest.placement == "header" else "tdnext.pane"
        command = "tool" if manifest.placement == "header" else "view"
        is_primary_broker_tab = broker_wasm is not None and manifest.placement == "header" and manifest.id == header_id
        broker_pane = ""
        if is_primary_broker_tab:
            assert broker_wasm is not None and header_id is not None
            wasm_location = json.dumps(f"file:{broker_wasm}")
            active = "true" if broker.active else "false"
            pane_size = 1 if broker.active else 12
            broker_pane = (
                f"        pane size={pane_size} borderless=true {{\n"
                f"            plugin location={wasm_location} {{\n"
                f"                header_id {json.dumps(tab_name(header_id))}\n"
                f"                active {json.dumps(active)}\n"
                "            }\n"
                "        }\n"
            )
        tabs.append(f'''tab name={name}{focused} {{
    pane split_direction="horizontal" {{
        pane size=2 borderless=true command={python} {{
            args "-m" "tdnext.plugins.desktop.panes" "header" {plugin_id}{snapshot}
        }}
{broker_pane}        pane split_direction="vertical" {{
            pane size=22 borderless=true command={python} {{
                args "-m" "tdnext.plugins.desktop.panes" "sidebar" {plugin_id}{snapshot}
            }}
            pane split_direction="horizontal" {{
                pane focus=true borderless=true command={python} {{
                    args "-m" "{module}" "{command}" {plugin_id}{snapshot}
                }}
                pane size=2 borderless=true command={python} {{
                    args "-m" "tdnext.plugins.desktop.panes" "footer" {plugin_id}{snapshot}
                }}
            }}
        }}
    }}
}}''')
    return "layout {\n" + "\n".join(tabs) + "\n}"


class DesktopPlugin:
    def run(self, context: PluginContext, views: Sequence[tuple[PluginManifest, object]]) -> None:
        textual = textual_views(views)
        stock = context.manager.executable("zellij")
        core_binary = core_capable_binary()
        if core_binary is not None:
            zellij: str | None = str(core_binary)
            core_active = True
        else:
            zellij = stock
            core_active = False
        size = shutil.get_terminal_size((80, 24))
        if zellij and "ZELLIJ" not in os.environ and size.columns >= 104 and size.lines >= 28:
            data = state_dir() / "zellij-data"
            data.mkdir(parents=True, exist_ok=True)
            try:
                env = {
                    **restored_env(),
                    "TDNEXT_ZELLIJ": zellij,
                    "TDNEXT_PYTHON": sys.executable,
                    "TDNEXT_DIRECTORY": str(context.directory),
                    "XDG_CACHE_HOME": str(state_dir() / "zellij-cache"),
                }
                env.pop(SESSION_ENV, None)
                env.pop(BROKER_ENABLE_ENV, None)
                env.pop(BROKER_HEADER_ENV, None)
                env.pop(CORE_ENABLE_ENV, None)
                env.pop(CORE_VERSION_ENV, None)
                header_id = primary_header_id(textual)
                if core_active:
                    env[CORE_ENABLE_ENV] = "1"
                    env[CORE_VERSION_ENV] = "2"
                    startup_broker = BrokerSnapshot(wasm=None, active=False)
                else:
                    startup_broker = broker_snapshot(header_id)
                    if startup_broker.wasm is not None and not broker_host_compatible(zellij):
                        startup_broker = BrokerSnapshot(wasm=None, active=False)
                    if startup_broker.active and header_id is not None:
                        env[BROKER_ENABLE_ENV] = "1"
                        env[BROKER_HEADER_ENV] = header_id
                sidebar_count = sum(manifest.placement == "sidebar" for manifest, _plugin in textual)
                bindings = "\n".join(
                    f'        bind "Alt {index}" {{ GoToTab {index}; }}'
                    if index <= sidebar_count
                    else f'        unbind "Alt {index}"'
                    for index in range(1, 10)
                )
                if header_id is None:
                    header_binding = '        unbind "Alt o"'
                elif core_active:
                    header_binding = (
                        '        bind "Alt o" {\n'
                        f"            OpenTabWithReturn {json.dumps(tab_name(header_id))};\n"
                        "        }"
                    )
                else:
                    # The Python Run binding is the initial Alt+o owner: it works
                    # with zero grants. After the user explicitly grants the
                    # broker, its runtime reconfigure overlays this same key
                    # (Zellij inserts replace per key), so Alt+o routes to the
                    # broker while every other binding is preserved. On denial
                    # or while cold, this binding stands and nothing is dead.
                    header_binding = (
                        '        bind "Alt o" {\n'
                        f'            Run {json.dumps(sys.executable)} "-m" "tdnext.plugins.desktop.shortcut" '
                        f"{json.dumps(header_id)} {{\n"
                        "                floating true\n"
                        "                close_on_exit true\n"
                        "            }\n"
                        "        }"
                    )
                template = Path(__file__).with_name("zellij.kdl").read_text(encoding="utf-8")
                with TemporaryDirectory(prefix="tdnext-zellij-") as directory:
                    config = Path(directory) / "zellij.kdl"
                    config.write_text(
                        template.replace("        // TDNEXT_SIDEBAR_BINDINGS", bindings).replace(
                            "        // TDNEXT_HEADER_BINDING", header_binding
                        ),
                        encoding="utf-8",
                    )
                    layout = layout_string(textual, context.session, startup_broker)
                    if textual:
                        tabs = layout.removeprefix("layout {\n").removesuffix("\n}").split("\ntab name=")
                        ordered = [view for view in textual if view[0].placement == "sidebar"] + [
                            view for view in textual if view[0].placement == "header"
                        ]
                        env["TDNEXT_TAB_LAYOUTS"] = json.dumps(
                            {
                                manifest.id: "layout {\n" + (tab if index == 0 else "tab name=" + tab) + "\n}"
                                for index, ((manifest, _), tab) in enumerate(zip(ordered, tabs, strict=True))
                            }
                        )
                    command = [zellij, "--config", str(config), "--data-dir", str(data), "--layout-string", layout]
                    result = subprocess.run(command, env=env, check=False)
            except OSError as exc:
                print(f"Could not launch Zellij: {exc}. Using fullscreen fallback.", file=sys.stderr)
            else:
                if result.returncode == 0:
                    return
                print(f"Zellij exited with status {result.returncode}. Using fullscreen fallback.", file=sys.stderr)
        DesktopApp(context, textual).run()
