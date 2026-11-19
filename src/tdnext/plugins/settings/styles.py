"""Terminal-cell layout and fixed neutral frames for Settings."""

SETTINGS_CSS = """
    SettingsView { height: 1fr; padding: 0 1; }
    SettingsView #settings-heading { height: 3; align-vertical: middle; }
    SettingsView #settings-title { width: 1fr; text-style: bold; }
    SettingsView #settings-back { width: 12; min-width: 8; height: 3; }
    SettingsView #settings-sections { height: 3; }
    SettingsView #settings-sections Button { height: 3; min-width: 13; width: auto; padding: 0 1; }
    SettingsView #settings-back, SettingsView #settings-sections Button, SettingsView #plugin-actions Button {
        border: solid $foreground-muted !important; background: $surface;
    }
    SettingsView #settings-back:hover, SettingsView #settings-back:focus,
    SettingsView #settings-sections Button:hover, SettingsView #settings-sections Button:focus,
    SettingsView #plugin-actions Button:hover, SettingsView #plugin-actions Button:focus {
        background: $boost; text-style: bold;
    }
    SettingsView #settings-pages, SettingsView .settings-page { height: 1fr; }
    SettingsView #settings-intro { height: 2; color: $text-muted; }
    SettingsView.narrow #settings-intro { height: 3; }
    SettingsView .settings-group { height: auto; }
    SettingsView #general-title { height: 2; text-style: bold; }
    SettingsView #general-help { height: auto; color: $text-muted; margin-top: 1; }
    SettingsView #plugin-list, SettingsView #plugin-list:focus { height: 1fr; border: solid $foreground-muted; }
    SettingsView #plugin-detail { height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    SettingsView #plugin-actions { height: 3; }
    SettingsView.narrow #plugin-actions { height: 4; }
    SettingsView #plugin-actions Button { width: 1fr; min-width: 11; height: 3; padding: 0; }
    SettingsView.narrow #plugin-actions Button { width: 13; }
    SettingsView #plugin-install.uninstall:enabled { color: $error; text-style: bold; }
    SettingsView #settings-feedback-scroll {
        height: auto; min-height: 3; max-height: 4; overflow-x: hidden; border: solid $foreground-muted;
    }
    SettingsView #settings-feedback-scroll:focus { background: $boost; text-style: bold; }
"""
