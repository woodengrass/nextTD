import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from native_processes import attached_session as _attached_session
from native_processes import monitor_pid, terminate_tagged

CLIENT_HEADER = ("CLIENT_ID", "ZELLIJ_PANE_ID", "RUNNING_COMMAND")


def wait_for(predicate, *, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for native desktop state")


def attached_session(tag: str, expected_tabs: int) -> str | None:
    return _attached_session(tag, expected_tabs)


def views_for_state(state_dir: Path, python: str) -> list[dict[str, object]]:
    try:
        import json as _json

        enabled = set(_json.loads((state_dir / "plugins.json").read_text(encoding="utf-8"))["enabled"])
    except (OSError, ValueError, KeyError):
        enabled = {"desktop", "files", "monitor", "settings"}
    sidebar = sorted(name for name in enabled if name not in {"desktop", "settings", "icons"})
    shortcuts = {
        "files": (
            "Enter open   Backspace up   ? help   v preview   b folders   g path   n folder   r rename   "
            "y/x/p copy/move   d delete"
        ),
        "monitor": "btop: q quit   arrows select   ? help",
        "shell": "Shell keys   Ctrl+g Zellij lock",
        "settings": "Esc Back   Tab Sections and controls",
    }
    views: list[dict[str, object]] = []
    for name in sidebar:
        views.append({
            "id": name, "name": name.capitalize(), "icon": name[0].upper() if name else "?",
            "placement": "sidebar", "command": python, "args": ["-m", "tdnext.pane", "view", name],
            "shortcuts": shortcuts.get(name, ""),
        })
    if "settings" in enabled:
        views.append({
            "id": "settings", "name": "Settings", "icon": "S", "placement": "header",
            "command": python, "args": ["-m", "tdnext.plugins.desktop.panes", "tool", "settings"],
            "shortcuts": "Esc back",
        })
    return views


class NativeSession:
    """Drive one isolated real desktop: Rust tdnext-desktop plus patched core.

    Each session owns a private tmux server, a private Zellij socket dir,
    and a private state dir (from the ``native_env`` fixture). Content
    programs are the original Python Files/Settings panes; the desktop
    chrome and navigation (Alt+o/Esc, bare ``tdnext-header:settings`` tab)
    come from the supported Rust core/desktop under test.
    """

    def __init__(
        self, root: Path, env: dict[str, str], binary: str, *, expected_tabs: int = 3, expected_panes: int | None = None
    ) -> None:
        self.env = env
        self.binary = binary
        desktop = env.get("TDNEXT_DESKTOP_BINARY", "")
        python = env.get("TDNEXT_PYTHON", sys.executable)
        if not desktop:
            root_dir = Path(__file__).parents[1]
            desktop = str(root_dir / "native-desktop" / "target" / "release" / "tdnext-desktop")
        self.desktop = desktop
        state_dir = Path(env["TDNEXT_STATE_DIR"])
        config_path = state_dir / "desktop.json"
        config_path.write_text(
            json.dumps({"views": views_for_state(state_dir, python)}), encoding="utf-8",
        )
        self.tmux_socket = env["TDNEXT_TEST_TMUX_SOCKET"]
        self.tmux_name = f"tdnext-{uuid.uuid4().hex}"
        subprocess.run(
            ["tmux", "-S", self.tmux_socket, "-f", "/dev/null", "new-session", "-d", "-s", self.tmux_name,
             "-x", "120", "-y", "36", "-e", f"TDNEXT_TEST_SESSION={self.tmux_name}",
             self.desktop, "--core", self.binary, "--config", str(config_path), str(root)],
            env=env, capture_output=True, check=True, timeout=5,
        )
        self.startup_diagnostics = "waiting for an attached native pane"
        try:
            self.name = wait_for(lambda: attached_session(self.tmux_name, expected_tabs))
            self.target_env = {**env, "ZELLIJ_SESSION_NAME": self.name}
            wait_for(lambda: self.ready(expected_tabs))
        except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
            try:
                screen = subprocess.run(
                    ["tmux", "-S", self.tmux_socket, "capture-pane", "-p", "-S", "-1000", "-t", self.tmux_name],
                    env=env, capture_output=True, text=True, check=False, timeout=5,
                )
                terminal = f"stdout={screen.stdout!r}, stderr={screen.stderr!r}"
            except (OSError, subprocess.SubprocessError) as diagnostic_error:
                terminal = f"capture failed: {diagnostic_error}"
            try:
                self.stop_tmux()
            finally:
                terminate_tagged(self.tmux_name)
            raise AssertionError(
                f"Native session did not attach: {self.startup_diagnostics}; "
                f"terminal={terminal}"
            ) from exc

    def ready(self, expected_tabs: int) -> bool:
        clients = subprocess.run(
            [self.binary, "action", "list-clients"], capture_output=True, text=True,
            env=self.target_env, check=False, timeout=5,
        )
        self.startup_diagnostics = (
            f"clients: exit={clients.returncode}, stdout={clients.stdout!r}, stderr={clients.stderr!r}"
        )
        lines = clients.stdout.splitlines()
        if clients.returncode or len(lines) < 2:
            return False
        if tuple(lines[0].split()) != CLIENT_HEADER:
            if clients.stdout.lstrip().startswith("["):
                return False
            raise AssertionError(f"Unexpected Zellij client response: {clients.stdout!r}")
        rows = [line.split() for line in lines[1:]]
        if not any(len(row) >= 2 and row[0].isdigit() and row[1].startswith(("terminal_", "plugin_")) for row in rows):
            return False
        tabs = subprocess.run(
            [self.binary, "action", "list-tabs", "--json"], capture_output=True, text=True,
            env=self.target_env, check=False, timeout=5,
        )
        self.startup_diagnostics += f"; tabs: exit={tabs.returncode}, stdout={tabs.stdout!r}, stderr={tabs.stderr!r}"
        if tabs.returncode or not tabs.stdout.strip():
            return False
        if tuple(tabs.stdout.splitlines()[0].split()) == CLIENT_HEADER:
            return False
        result = json.loads(tabs.stdout)
        return len(result) >= expected_tabs and any(tab["active"] for tab in result)

    def send_key(self, key: str) -> None:
        subprocess.run(
            ["tmux", "-S", self.tmux_socket, "send-keys", "-t", self.tmux_name, key],
            env=self.env, capture_output=True, check=True, timeout=5,
        )

    def click_header(self, x: int = 110) -> None:
        data = f"\x1b[<0;{x};1M\x1b[<0;{x};1m".encode()
        subprocess.run(
            ["tmux", "-S", self.tmux_socket, "send-keys", "-t", self.tmux_name, "-H", *[f"{b:02x}" for b in data]],
            env=self.env, capture_output=True, check=True, timeout=5,
        )

    def header_mouse(self, x: int, *, release: bool) -> None:
        self.mouse(x, 1, release=release)

    def mouse(self, x: int, y: int, *, release: bool) -> None:
        data = f"\x1b[<0;{x};{y}{'m' if release else 'M'}".encode()
        subprocess.run(
            ["tmux", "-S", self.tmux_socket, "send-keys", "-t", self.tmux_name, "-H", *[f"{b:02x}" for b in data]],
            env=self.env, capture_output=True, check=True, timeout=5,
        )

    def stop_tmux(self) -> None:
        subprocess.run(
            ["tmux", "-S", self.tmux_socket, "kill-session", "-t", self.tmux_name],
            env=self.env, capture_output=True, check=False, timeout=5,
        )

    def sessions(self) -> set[str]:
        result = subprocess.run(
            [self.binary, "list-sessions", "--no-formatting"],
            capture_output=True, text=True, env=self.env, check=False, timeout=5,
        )
        return {line.split()[0] for line in result.stdout.splitlines() if line.strip()}

    def action(self, *args: str) -> str:
        query = args[0] in {"list-clients", "list-panes", "list-tabs", "dump-screen"}
        last_result = "not queried"

        def response() -> tuple[str] | None:
            nonlocal last_result
            result = subprocess.run(
                [self.binary, "action", *args], capture_output=True, text=True,
                env=self.target_env, check=False, timeout=5,
            )
            last_result = f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
            if query and (
                (result.returncode == 0 and not result.stdout.strip() and args[0] != "dump-screen")
                or (result.returncode != 0 and result.stderr.strip() == "There is no active session!")
            ):
                return None
            if result.returncode:
                raise AssertionError(f"Zellij action {args!r} failed: {last_result}")
            return (result.stdout,)

        try:
            return wait_for(response)[0]
        except AssertionError as exc:
            screen = subprocess.run(
                ["tmux", "-S", self.tmux_socket, "capture-pane", "-p", "-t", self.tmux_name],
                env=self.env, capture_output=True, text=True, check=False, timeout=5,
            )
            raise AssertionError(
                f"Zellij action {args!r}: {last_result}; sessions={self.sessions()!r}, "
                f"terminal={screen.stdout!r}, terminal_error={screen.stderr!r}"
            ) from exc

    def tabs(self) -> list[dict[str, str | int | bool]]:
        return json.loads(self.action("list-tabs", "--json"))

    def tab_id(self, name: str) -> int:
        return next(int(tab["tab_id"]) for tab in self.tabs() if str(tab["name"]).split("@", 1)[0] == name)

    def active_tab(self) -> int | None:
        return next((int(tab["tab_id"]) for tab in self.tabs() if tab["active"]), None)

    def switch(self, name: str) -> None:
        target = self.tab_id(name)
        self.send_key({"Files": "M-1", "Monitor": "M-2", "Shell": "M-3"}[name])
        wait_for(lambda: self.active_tab() == target)

    def open_settings(self) -> None:
        self.send_key("M-o")
        self.wait_for_settings()

    def wait_for_settings(self) -> None:
        target = self.tab_id("tdnext-header:settings")
        wait_for(lambda: self.active_tab() == target)
        last_state = "Settings tool pane not found"

        def settings_ready() -> bool:
            nonlocal last_state
            output = self.action("list-panes", "--all", "--json")
            if not output.strip():
                last_state = "No panes returned"
                return False
            panes = json.loads(output)
            tool = next((pane for pane in panes if "panes tool settings" in (pane.get("pane_command") or "")), None)
            if tool is None:
                last_state = f"Settings tool pane missing: {output}"
                return False
            pane_id = f"terminal_{tool['id']}"
            clients = self.action("list-clients")
            screen = self.action("dump-screen", "--pane-id", pane_id)
            last_state = f"pane={pane_id}; clients={clients!r}; screen={screen!r}"
            general = "> General" in screen and "Provider:" in screen and "Style:" in screen
            plugins = "> Plugins" in screen and "Plugin" in screen and "State" in screen and "Install" in screen
            return pane_id in clients and "Settings" in screen and "Back" in screen and (general or plugins)

        try:
            wait_for(settings_ready)
        except AssertionError as exc:
            raise AssertionError(f"Settings pane did not become ready: {last_state}") from exc

    def back_to(self, name: str) -> None:
        target = self.tab_id(name)
        panes = json.loads(self.action("list-panes", "--all", "--json"))
        tool = next(pane for pane in panes if "panes tool settings" in (pane.get("pane_command") or ""))
        pane_id = f"terminal_{tool['id']}"
        self.send_key("Escape")

        def returned() -> bool:
            if self.active_tab() == target:
                return True
            screen = self.action("dump-screen", "--pane-id", pane_id)
            errors = ("Zellij action failed", "Cannot identify", "previous pane", "previous tab")
            if any(error in screen for error in errors):
                raise AssertionError(f"Settings reported an error: {screen}")
            return False

        try:
            wait_for(returned)
        except AssertionError as exc:
            screen = subprocess.run(
                ["tmux", "-S", self.tmux_socket, "capture-pane", "-p", "-t", self.tmux_name],
                env=self.env, capture_output=True, text=True, check=False, timeout=5,
            )
            raise AssertionError(
                f"Escape did not return to {name}: tabs={self.tabs()!r}, clients={self.action('list-clients')!r}, "
                f"terminal={screen.stdout!r}"
            ) from exc

    def btop_pid(self) -> int | None:
        return monitor_pid(self.name, self.tmux_name)

    def close(self) -> None:
        try:
            result = subprocess.run(
                [self.binary, "kill-session", self.name], env=self.env, capture_output=True, check=False, timeout=5
            )
        finally:
            try:
                self.stop_tmux()
            finally:
                terminate_tagged(self.tmux_name)
        if result.returncode:
            raise AssertionError(f"Cannot stop Zellij session {self.name}: {result.stderr!r}")
