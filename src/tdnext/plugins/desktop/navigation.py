import os
import re
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import overload as typing_overload

from pydantic import BaseModel, TypeAdapter, ValidationError

from tdnext.core.errors import PluginError


class TabInfo(BaseModel):
    tab_id: int
    name: str


class PaneInfo(BaseModel):
    id: int
    is_plugin: bool
    is_floating: bool
    tab_id: int


@typing_overload
def gather[T](task: Callable[[], T]) -> tuple[T]: ...


@typing_overload
def gather[T, T2](task: Callable[[], T], task2: Callable[[], T2]) -> tuple[T, T2]: ...


@typing_overload
def gather[T, T2, T3](
    task: Callable[[], T], task2: Callable[[], T2], task3: Callable[[], T3]
) -> tuple[T, T2, T3]: ...


def gather(
    task: Callable[[], object],
    task2: Callable[[], object] | None = None,
    task3: Callable[[], object] | None = None,
) -> tuple[object, ...]:
    """Run independent queries concurrently and collect results in order.

    Retained as a tested utility only. Production Zellij control queries
    (open_header, return_to_origin) must stay sequential: concurrent
    `zellij action` subprocesses can receive each other's stdout, which
    cross-wires responses. Results are collected in submission order so
    error precedence matches the original sequential code.
    """
    tasks = [entry for entry in (task, task2, task3) if entry is not None]
    if len(tasks) == 1:
        return (tasks[0](),)
    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="tdnext-nav") as pool:
        futures = [pool.submit(entry) for entry in tasks]
    return tuple(future.result() for future in futures)


def tab_name(plugin_id: str) -> str:
    return f"tdnext-header:{plugin_id}"


def matches_tab(tab: TabInfo, plugin_id: str) -> bool:
    name = tab_name(plugin_id)
    return tab.name == name or tab.name.startswith(f"{name}@")


def action(*arguments: str) -> str:
    query = arguments[0] in {"list-clients", "list-panes", "list-tabs"}
    deadline = time.monotonic() + 5
    while True:
        try:
            result = subprocess.run(
                [os.environ["TDNEXT_ZELLIJ"], "action", *arguments],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
            raise PluginError(f"Cannot control Zellij: {exc}") from exc
        pending = query and (
            (result.returncode == 0 and not result.stdout.strip())
            or (result.returncode != 0 and result.stderr.strip() == "There is no active session!")
        )
        if not pending:
            if result.returncode:
                raise PluginError(f"Zellij action failed: {result.stderr.strip()}")
            return result.stdout
        if time.monotonic() >= deadline:
            detail = result.stderr.strip() or "empty output"
            raise PluginError(f"Zellij query {arguments[0]} did not become ready: {detail}")
        time.sleep(0.05)


def client_panes() -> dict[str, str]:
    lines = action("list-clients").splitlines()
    return {columns[0]: columns[1] for line in lines[1:] if len(columns := line.split(maxsplit=2)) >= 2}


BROKER_BACK_PIPE = "back"
BROKER_FORGET_PIPE = "forget"
BROKER_PIPE_TIMEOUT = 5
BROKER_ENABLE_ENV = "TDNEXT_NAVIGATOR_BROKER"
BROKER_HEADER_ENV = "TDNEXT_NAVIGATOR_HEADER"
CORE_ENABLE_ENV = "TDNEXT_CORE_NAVIGATION"
CORE_VERSION_ENV = "TDNEXT_CORE_NAVIGATION_VERSION"


def _core_enabled() -> bool:
    """Whether this session was launched on the native core navigation binary.

    Only the native launcher sets TDNEXT_CORE_NAVIGATION; every other
    session (including all pre-core sessions) keeps the original stock or
    experimental broker routes with no core CLI call.
    """
    return os.environ.get(CORE_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def restore_header_focus() -> None:
    if _core_enabled() and os.environ.get(CORE_VERSION_ENV) == "2":
        action("restore-focus-from-header")
    else:
        action("focus-last-pane")


def _broker_enabled() -> bool:
    """Whether this session was launched with the return broker.

    Only the native launcher sets TDNEXT_NAVIGATOR_BROKER; every other
    session (including all pre-broker sessions) takes the original
    tab-name origin path with no broker pipe call and no extra queries.
    """
    return os.environ.get(BROKER_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _broker_header_id() -> str:
    """Primary header id for broker coordination, or empty when unscoped."""
    return os.environ.get(BROKER_HEADER_ENV, "").strip()


def _broker_for_header(plugin_id: str) -> bool:
    """Whether the broker hybrid path applies to this header tab.

    Ordered coordination only (not atomic): the broker is consulted for the
    primary header only. When TDNEXT_NAVIGATOR_HEADER is unset or empty
    (pre-hybrid sessions and existing tests) the broker flag alone gates, so
    flag-off behavior is untouched and legacy sequences are preserved. When
    the header env is set, only the matching plugin id uses the broker;
    secondary headers keep the original tab-name path with no pipe call,
    even with the flag enabled.
    """
    if not _broker_enabled():
        return False
    header_id = _broker_header_id()
    if not header_id:
        return True
    return plugin_id == header_id


def _broker_forget(client: str) -> None:
    """Ask the broker to drop this client's recorded origin before reopening.

    Ordered coordination only (not atomic): the acknowledged forget must
    precede the suffix rename/switch, but tabs or panes can still change in
    between, so callers must treat a successful forget plus a later Zellij
    failure as an ordered-but-partial handoff, never as an atomic switch.
    Fails closed on any transport failure, timeout, nonzero exit, empty or
    ambiguous reply, broker err:, or any ok: line that is not exactly
    ``ok:FORGOT CLIENT=<client>`` for the selected client.
    """
    try:
        result = subprocess.run(
            [
                os.environ["TDNEXT_ZELLIJ"], "action", "pipe",
                "--name", BROKER_FORGET_PIPE, "--", f"client={client}",
            ],
            capture_output=True, text=True, check=False, timeout=BROKER_PIPE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
        raise PluginError(f"Cannot contact the header forget pipe: {exc}") from exc
    if result.returncode:
        raise PluginError(f"Header forget pipe failed: {result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PluginError(f"Header forget pipe gave {len(lines)} usable replies; expected exactly one.")
    reply = lines[0].strip()
    expected = f"ok:FORGOT CLIENT={client}"
    if reply != expected:
        if reply.startswith("err:"):
            raise PluginError(reply[len("err:"):] or "The header forget was rejected.")
        raise PluginError(f"Header forget pipe gave an unexpected reply: {reply!r}.")


def open_header(plugin_id: str) -> None:
    if _core_enabled():
        action("open-tab-with-return-from-header", tab_name(plugin_id))
        return
    try:
        clients = client_panes()
        source = os.environ.get("ZELLIJ_PANE_ID")
        candidates = [client for client, pane in clients.items() if source is None or pane == f"terminal_{source}"]
        if len(candidates) != 1:
            raise PluginError("Cannot identify the client opening the header.")
        client = candidates[0]
        # Mouse clicks and the shortcut's Run pane both focus shell chrome.
        if source is not None:
            action("focus-last-pane")
        focused_pane = client_panes()[client]
        panes_text = action("list-panes", "--json")
        tabs_text = action("list-tabs", "--json")
        panes = TypeAdapter(list[PaneInfo]).validate_json(panes_text)
        if source is not None and focused_pane == f"terminal_{source}" and any(
            not pane.is_plugin and pane.id == int(source) and pane.is_floating for pane in panes
        ):
            action("toggle-floating-panes")
            focused_pane = client_panes()[client]
        kind, pane_number = focused_pane.split("_", 1)
        if kind not in {"terminal", "plugin"}:
            raise PluginError("Cannot identify the focused pane.")
        origin = next(pane for pane in panes if pane.id == int(pane_number) and pane.is_plugin == (kind == "plugin"))
        origin_id = origin.tab_id
        tabs = TypeAdapter(list[TabInfo]).validate_json(tabs_text)
        destination = next(tab for tab in tabs if matches_tab(tab, plugin_id))
    except (KeyError, ValidationError, ValueError, StopIteration) as exc:
        raise PluginError(f"Cannot locate the current tab or {plugin_id} tab in this session.") from exc
    if origin_id == destination.tab_id:
        return
    if _broker_for_header(plugin_id):
        _broker_forget(client)
    kind = "plugin" if origin.is_plugin else "terminal"
    action("rename-tab-by-id", str(destination.tab_id), f"{tab_name(plugin_id)}@{origin_id}:{kind}_{origin.id}")
    action("go-to-tab-by-id", str(destination.tab_id))


def _back_target() -> tuple[str, str]:
    """Select the unique returning client and its source pane.

    Uses the same client-identity rule as open_header/return_to_origin: the
    current ZELLIJ_PANE_ID must single out exactly one client from
    list-clients. Anything else is ambiguous and no pipe call is made.
    """
    clients = client_panes()
    current = os.environ.get("ZELLIJ_PANE_ID")
    if current is None:
        if len(clients) != 1:
            raise PluginError("Cannot identify the client returning from the header.")
        client = next(iter(clients))
        source = clients[client]
    else:
        wanted = f"terminal_{current}"
        candidates = [name for name, pane in clients.items() if pane == wanted]
        if len(candidates) != 1:
            raise PluginError("Cannot identify the client returning from the header.")
        client = candidates[0]
        source = wanted
    kind, separator, number = source.partition("_")
    if not separator or kind not in {"terminal", "plugin"} or not number.isdigit():
        raise PluginError("Cannot identify the client returning from the header.")
    return client, source


def _broker_back(client: str, source: str) -> None:
    """Ask the return broker to serve this client's explicit back claim.

    Fails closed: transport failure, timeout, empty or ambiguous replies,
    unexpected replies, and every err: rejection surface as PluginError with
    no navigation here. The single exact ``NO ORIGIN FOR CLIENT=<client>``
    fallback to the legacy suffix path is decided by back_from_header, which
    compares the selected client exactly; this helper never falls back.
    """
    try:
        result = subprocess.run(
            [
                os.environ["TDNEXT_ZELLIJ"], "action", "pipe",
                "--name", BROKER_BACK_PIPE, "--", f"client={client} source={source}",
            ],
            capture_output=True, text=True, check=False, timeout=BROKER_PIPE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
        raise PluginError(f"Cannot contact the header return pipe: {exc}") from exc
    if result.returncode:
        raise PluginError(f"Header return pipe failed: {result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PluginError(f"Header return pipe gave {len(lines)} usable replies; expected exactly one.")
    reply = lines[0].strip()
    if re.fullmatch(rf"ok:RETURN client={re.escape(client)} pane=(?:terminal|plugin)_[0-9]+", reply):
        return
    if reply.startswith("err:"):
        raise PluginError(reply[len("err:"):] or "The header return was rejected.")
    raise PluginError(f"Header return pipe gave an unexpected reply: {reply!r}.")


def back_from_header(plugin_id: str) -> None:
    """Return from a header tab, via the broker only for the primary header.

    Ordered coordination only (not atomic): without the broker flag, or for
    any secondary header when TDNEXT_NAVIGATOR_HEADER is set, this is
    exactly the original tab-name origin path with no broker pipe call and
    no extra queries. For the primary header with the flag, the broker claim
    must succeed outright; anything else fails closed with a visible error
    and no navigation, except the single exact
    ``err:NO ORIGIN FOR CLIENT=<selected>`` verdict for the selected client,
    which falls back to the legacy tab-name suffix path. Empty, malformed,
    duplicate, other-client, timeout, transport, and all other broker errors
    never fall back.
    """
    if _core_enabled():
        action("return-to-origin")
        return
    if not _broker_for_header(plugin_id):
        return_to_origin(plugin_id)
        return
    client, source = _back_target()
    try:
        _broker_back(client, source)
    except PluginError as exc:
        if str(exc) == f"NO ORIGIN FOR CLIENT={client}":
            return_to_origin(plugin_id)
            return
        raise


def return_to_origin(plugin_id: str) -> None:
    try:
        tabs = TypeAdapter(list[TabInfo]).validate_json(action("list-tabs", "--json"))
        destination = next(tab for tab in tabs if matches_tab(tab, plugin_id))
        origin_text, separator, pane_id = destination.name.rsplit("@", 1)[1].partition(":")
        origin = int(origin_text)
    except (IndexError, StopIteration, ValueError, ValidationError) as exc:
        raise PluginError("No previous tab is recorded; choose a workspace in the sidebar.") from exc
    if not any(tab.tab_id == origin for tab in tabs):
        raise PluginError("The previous tab has closed; choose a workspace in the sidebar.")
    client: str | None = None
    if separator:
        try:
            source = os.environ["ZELLIJ_PANE_ID"]
            clients_text = client_panes()
            panes_text = action("list-panes", "--json")
            clients = [client for client, pane in clients_text.items() if pane == f"terminal_{source}"]
            if len(clients) != 1:
                raise PluginError("Cannot identify the client returning from the header.")
            client = clients[0]
            kind, pane_number = pane_id.split("_", 1)
            panes = TypeAdapter(list[PaneInfo]).validate_json(panes_text)
            pane_exists = any(
                pane.tab_id == origin and pane.id == int(pane_number) and pane.is_plugin == (kind == "plugin")
                for pane in panes
            )
        except (KeyError, ValueError, ValidationError) as exc:
            raise PluginError("The previous pane could not be identified.") from exc
        if kind not in {"terminal", "plugin"} or not pane_exists:
            raise PluginError("The previous pane has closed; choose a workspace in the sidebar.")
    action("go-to-tab-by-id", str(origin))
    if client is not None and client_panes().get(client) != pane_id:
        action("focus-pane-id", pane_id)
