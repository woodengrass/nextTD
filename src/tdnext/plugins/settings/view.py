from functools import partial
from typing import Literal

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, ContentSwitcher, DataTable, Select, Static

from tdnext.api import CloseView, PluginContext
from tdnext.core.errors import InstallationConsentRequired, PluginError
from tdnext.core.graph import dependants
from tdnext.core.models import PluginState
from tdnext.plugins.settings.styles import SETTINGS_CSS

from .appearance import AppearanceControls
from .dialogs import ConfirmPluginChange
from .editor import EditorControls
from .general_focus import GeneralFocus
from .operations import Operation, OperationResult, apply_operation, protected_plugin_ids

Section = Literal["general", "plugins"]


class SettingsView(GeneralFocus):
    BINDINGS = [
        ("escape", "back", "Back"),
        ("i", "install", "Install"),
        ("e", "enable", "Enable"),
        ("d", "disable", "Disable"),
        ("u", "uninstall", "Uninstall"),
    ]
    DEFAULT_CSS = SETTINGS_CSS

    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.context = context
        self.plugin_ids = sorted(context.manager.manifests)
        self.busy = False
        self.section: Section = "general"
        self.appearance_feedback = "Icons: restart. Editor: next file open."
        self.plugin_feedback = "Changes apply after restarting the desktop."

    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-heading"):
            yield Static("Settings", id="settings-title")
            yield Button("Back", id="settings-back")
        with Horizontal(id="settings-sections"):
            yield Button("> General", id="section-general")
            yield Button("  Plugins", id="section-plugins")
        with ContentSwitcher(initial="settings-general", id="settings-pages"):
            with VerticalScroll(id="settings-general", classes="settings-page"):
                with Vertical(id="appearance-group", classes="settings-group"):
                    yield Static("Icon appearance", id="general-title")
                    yield AppearanceControls(self.context)
                    yield Static(
                        "Compatibility uses plain text. Codicons requires a compatible Nerd Font.", id="general-help"
                    )
                with Vertical(id="editor-group", classes="settings-group"):
                    yield Static("File editor", id="editor-title")
                    yield EditorControls(self.context)
            with Vertical(id="settings-plugins", classes="settings-page"):
                yield Static(
                    "Manage trusted local plugins.\ni Install  e Enable  d Disable   u Uninstall",
                    id="settings-intro", markup=False,
                )
                yield DataTable(id="plugin-list", cursor_type="row")
                yield Static(id="plugin-detail", markup=False)
                with HorizontalScroll(id="plugin-actions"):
                    yield Button("Install", id="plugin-install")
                    yield Button("Enable", id="plugin-enable")
                    yield Button("Disable", id="plugin-disable")
                    yield Button("Uninstall", id="plugin-uninstall")
        with VerticalScroll(id="settings-feedback-scroll"):
            yield Static("Changes apply after restarting the desktop.", id="plugin-feedback", markup=False)

    def on_mount(self) -> None:
        self.query_one("#settings-general", VerticalScroll).can_focus = False
        self.query_one("#plugin-actions", HorizontalScroll).can_focus = False
        for button in self.query("#settings-sections Button, #plugin-actions Button"):
            if isinstance(button, Button):
                button.active_effect_duration = 0
        table = self.query_one("#plugin-list", DataTable)
        table.add_columns("Plugin", "State")
        self.refresh_plugins()
        self.set_section("general")

    def on_show(self) -> None:
        self.refresh_plugins()
        self.set_section("general")

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 46, "narrow")

    def set_section(self, section: Section) -> None:
        self.section = section
        self.query_one("#settings-pages", ContentSwitcher).current = f"settings-{section}"
        self.query_one("#section-general", Button).label = "> General" if section == "general" else "  General"
        self.query_one("#section-plugins", Button).label = "> Plugins" if section == "plugins" else "  Plugins"
        self.show_feedback(self.appearance_feedback if section == "general" else self.plugin_feedback)
        if section == "general":
            self.refresh_general()
            self.query_one("#icon-provider", Select).focus()
        else:
            self.query_one("#plugin-list", DataTable).focus()

    def on_native_activation(self) -> None:
        try:
            self.refresh_general()
        except PluginError as exc:
            self.notify(f"Cannot refresh settings: {exc}", severity="error")

    def on_appearance_controls_feedback(self, event: AppearanceControls.Feedback) -> None:
        self.appearance_feedback = event.text
        if self.section == "general":
            self.show_feedback(event.text)

    def on_editor_controls_feedback(self, event: EditorControls.Feedback) -> None:
        self.appearance_feedback = event.text
        if self.section == "general":
            self.show_feedback(event.text)

    def show_feedback(self, text: str) -> None:
        self.query_one("#plugin-feedback", Static).update(text)
        self.query_one("#settings-feedback-scroll", VerticalScroll).scroll_home(animate=False)

    def update_plugin_feedback(self, text: str) -> None:
        self.plugin_feedback = text
        if self.section == "plugins":
            self.show_feedback(text)

    def selected_plugin(self) -> str | None:
        row = self.query_one("#plugin-list", DataTable).cursor_row
        return self.plugin_ids[row] if 0 <= row < len(self.plugin_ids) else None

    def refresh_plugins(self) -> None:
        table = self.query_one("#plugin-list", DataTable)
        row = table.cursor_row
        selected = self.selected_plugin()
        state = self.context.manager.store.read()
        errors = getattr(self.context.manager.manifests, "errors", {})
        self.plugin_ids = sorted(self.context.manager.manifests.keys() | state.installed | errors.keys())
        table.clear()
        for plugin_id in self.plugin_ids:
            manifest = self.context.manager.manifests.get(plugin_id)
            if manifest is None:
                status = "Enabled" if plugin_id in state.enabled else (
                    "Installed" if plugin_id in state.installed else "Invalid"
                )
                table.add_row(f"{plugin_id} (invalid)" if plugin_id in errors else f"{plugin_id} (missing)", status,
                              key=plugin_id)
                continue
            status = "Enabled" if plugin_id in state.enabled else (
                "Installed" if plugin_id in state.installed else "Available"
            )
            table.add_row(manifest.name, status, key=plugin_id)
        if self.plugin_ids:
            table.move_cursor(row=self.plugin_ids.index(selected) if selected in self.plugin_ids else min(
                row, len(self.plugin_ids) - 1
            ))
        self.refresh_selection(state)

    def refresh_selection(self, state: PluginState) -> None:
        target = self.selected_plugin()
        if target is None:
            self.query_one("#plugin-detail", Static).update("No plugins found.")
            for button in self.query("#plugin-actions Button"):
                button.disabled = True
            return
        manifest = self.context.manager.manifests.get(target)
        protected_ids = protected_plugin_ids(self.context.manager, self.context.session)
        protected = target in protected_ids or bool(
            dependants(self.context.manager.manifests, target, state.enabled) & protected_ids
        )
        errors = getattr(self.context.manager.manifests, "errors", {})
        detail = manifest.description if manifest is not None else errors.get(
            target, "Plugin manifest is missing; disable or uninstall."
        )
        if protected:
            detail = f"Protected (running Desktop / Settings): {detail}"
        self.query_one("#plugin-detail", Static).update(detail)
        self.query_one("#plugin-install", Button).disabled = self.busy or manifest is None or target in state.installed
        self.query_one("#plugin-enable", Button).disabled = self.busy or manifest is None or target in state.enabled
        self.query_one("#plugin-disable", Button).disabled = self.busy or target not in state.enabled or protected
        self.query_one("#plugin-uninstall", Button).disabled = self.busy or target not in state.installed or protected

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self.refresh_selection(self.context.manager.store.read())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions: dict[str, Operation] = {
            "plugin-install": "install", "plugin-enable": "enable",
            "plugin-disable": "disable", "plugin-uninstall": "uninstall",
        }
        sections: dict[str, Section] = {"section-general": "general", "section-plugins": "plugins"}
        if event.button.id == "settings-back":
            self.action_back()
        elif event.button.id in sections:
            self.set_section(sections[event.button.id])
        elif event.button.id in actions:
            self.request_operation(actions[event.button.id])

    def request_operation(self, operation: Operation) -> None:
        if self.section != "plugins":
            return
        target = self.selected_plugin()
        if target is None or self.query_one(f"#plugin-{operation}", Button).disabled:
            return
        affected = dependants(self.context.manager.manifests, target, self.context.manager.store.read().enabled)
        others = sorted(affected - {target})
        if operation == "uninstall" or (operation == "disable" and others):
            details = f"{target}: " + (f"also disables {', '.join(others)}." if others else "remove installed state.")
            self.app.push_screen(
                ConfirmPluginChange(f"{operation.title()} {target}?", details, operation.title()),
                partial(self.confirm_operation, operation, target),
            )
            return
        self.confirm_operation(operation, target, True)

    def confirm_operation(self, operation: Operation, target: str, confirmed: bool | None) -> None:
        if not confirmed or self.busy:
            return
        self.busy = True
        progress = {"install": "Installing", "enable": "Enabling", "disable": "Disabling", "uninstall": "Uninstalling"}
        self.update_plugin_feedback(f"{progress[operation]} {target}…")
        self.refresh_selection(self.context.manager.store.read())
        self.run_operation(operation, target)

    @work(thread=True)
    def run_operation(
        self, operation: Operation, target: str, approved: tuple[tuple[str, str], ...] = ()
    ) -> None:
        try:
            result = apply_operation(self.context.manager, operation, target, approved, self.context.session)
        except InstallationConsentRequired as exc:
            self.app.call_from_thread(self.request_install_consent, operation, target, exc)
        except PluginError as exc:
            self.app.call_from_thread(self.operation_failed, str(exc))
        else:
            self.app.call_from_thread(self.operation_succeeded, result)

    def request_install_consent(self, operation: Operation, target: str, error: InstallationConsentRequired) -> None:
        self.app.push_screen(
            ConfirmPluginChange("Install external tools?", str(error), "Install"),
            partial(self.install_consent_result, operation, target, error.actions),
        )

    def install_consent_result(
        self, operation: Operation, target: str, actions: tuple[tuple[str, str], ...], confirmed: bool | None
    ) -> None:
        if confirmed:
            self.run_operation(operation, target, actions)
        else:
            self.operation_failed("Installation declined; plugin state was not changed.")

    def operation_succeeded(self, result: OperationResult) -> None:
        self.busy = False
        names = ", ".join(result.affected) or "none"
        self.update_plugin_feedback(
            f"{result.operation.title()}: {result.target}; affected: {names}. Restart desktop to apply."
        )
        self.refresh_plugins()
        self.query_one(AppearanceControls).refresh_providers()

    def operation_failed(self, message: str) -> None:
        self.busy = False
        self.update_plugin_feedback(f"Operation failed: (Tab to message; arrows scroll) {message}")
        self.refresh_selection(self.context.manager.store.read())

    def action_back(self) -> None:
        self.post_message(CloseView())

    def plugin_shortcuts_available(self) -> bool:
        focused = self.app.focused
        return self.section == "plugins" and (
            focused is None or not any(isinstance(widget, Select) for widget in (focused, *focused.ancestors))
        )

    def action_install(self) -> None:
        if self.plugin_shortcuts_available():
            self.request_operation("install")

    def action_enable(self) -> None:
        if self.plugin_shortcuts_available():
            self.request_operation("enable")

    def action_disable(self) -> None:
        if self.plugin_shortcuts_available():
            self.request_operation("disable")

    def action_uninstall(self) -> None:
        if self.plugin_shortcuts_available():
            self.request_operation("uninstall")
