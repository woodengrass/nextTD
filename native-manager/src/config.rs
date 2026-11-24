//! Session config rendering: icons, footer shortcuts, and desktop.json.

use std::collections::{BTreeSet, HashMap};

use crate::icons::{Appearance, resolve as resolve_icon};
use crate::run::ViewSpec;

/// Codicons apply only when a provider is selected and still enabled.
#[must_use]
pub fn use_codicons(appearance: &Appearance, enabled: &BTreeSet<String>) -> bool {
    appearance.codicons
        && appearance
            .provider_id
            .as_deref()
            .is_some_and(|provider| enabled.contains(provider))
}

/// Resolve one icon per enabled view from the provider appearance.
#[must_use]
pub fn session_icons(
    views: &[ViewSpec],
    appearance: &Appearance,
    enabled: &BTreeSet<String>,
) -> HashMap<String, String> {
    let provider_active =
        appearance.provider_id.as_deref() == Some("icons") && enabled.contains("icons");
    let codicons = use_codicons(appearance, enabled);
    let mut icons = HashMap::new();
    for view in views {
        let icon = match (provider_active, view.icon_key.as_deref()) {
            (true, Some(key)) => resolve_icon(key, codicons, None),
            _ => view.icon.as_deref().unwrap_or("[]").to_string(),
        };
        icons.insert(view.id.clone(), icon);
    }
    icons
}

/// Footer shortcuts each view advertises; custom views show none.
#[must_use]
pub fn shortcuts_for(id: &str) -> &str {
    match id {
        "files" => {
            "Enter open   Backspace up   ? help   v preview   b folders   g path   n folder   \
             r rename   y/x/p copy/move   d delete"
        }
        "monitor" => "btop: q quit   arrows select   ? help",
        "shell" => "Shell keys   Ctrl+g Zellij lock",
        "settings" => "Esc Back   Tab Sections and controls",
        _ => "",
    }
}

/// One desktop.json view entry.
pub fn view_entry(spec: &ViewSpec, icon: &str, tdnext_exe: &str) -> serde_json::Value {
    serde_json::json!({
        "id": spec.id,
        "name": spec.name,
        "icon": icon,
        "shortcuts": shortcuts_for(&spec.id),
        "placement": spec.placement,
        "command": tdnext_exe,
        "args": ["run", spec.id],
        "enabled": true,
    })
}

/// Assemble the session config; views arrive sorted by id from the catalog.
pub fn build_config(
    views: &[ViewSpec],
    icons: &HashMap<String, String>,
    tdnext_exe: &str,
) -> serde_json::Value {
    let entries: Vec<serde_json::Value> = views
        .iter()
        .map(|view| {
            let icon = icons.get(&view.id).map(String::as_str).unwrap_or("[]");
            view_entry(view, icon, tdnext_exe)
        })
        .collect();
    serde_json::json!({ "views": entries })
}

#[cfg(test)]
mod tests {
    use super::{build_config, session_icons, shortcuts_for, use_codicons, view_entry};
    use crate::icons::Appearance;
    use crate::run::ViewSpec;
    use anyhow::Result;
    use std::collections::{BTreeSet, HashMap};

    fn view(id: &str) -> ViewSpec {
        ViewSpec {
            id: id.to_string(),
            name: id.to_string(),
            placement: "sidebar".to_string(),
            command: Some(format!("tdnext-{id}")),
            args: Vec::new(),
            icon: Some("[?]".to_string()),
            icon_key: None,
            role: "desktop.view".to_string(),
            view_interfaces: Vec::new(),
        }
    }

    #[test]
    fn config_entries_launch_views_through_run() -> Result<()> {
        let views = vec![view("files"), view("monitor"), view("settings")];
        let mut icons = HashMap::new();
        icons.insert("files".to_string(), "[F]".to_string());
        icons.insert("monitor".to_string(), "[M]".to_string());
        icons.insert("settings".to_string(), "[S]".to_string());
        let config = build_config(&views, &icons, "/dist/tdnext");
        assert_eq!(
            config,
            serde_json::json!({ "views": [
                {
                    "id": "files", "name": "files", "icon": "[F]",
                    "shortcuts": "Enter open   Backspace up   ? help   v preview   b folders   g path   n folder   r rename   y/x/p copy/move   d delete",
                    "placement": "sidebar", "command": "/dist/tdnext",
                    "args": ["run", "files"], "enabled": true,
                },
                {
                    "id": "monitor", "name": "monitor", "icon": "[M]",
                    "shortcuts": "btop: q quit   arrows select   ? help",
                    "placement": "sidebar", "command": "/dist/tdnext",
                    "args": ["run", "monitor"], "enabled": true,
                },
                {
                    "id": "settings", "name": "settings", "icon": "[S]",
                    "shortcuts": "Esc Back   Tab Sections and controls",
                    "placement": "sidebar", "command": "/dist/tdnext",
                    "args": ["run", "settings"], "enabled": true,
                },
            ] })
        );
        Ok(())
    }

    #[test]
    fn custom_views_advertise_no_shortcuts() -> Result<()> {
        let entry = view_entry(&view("app"), "[]", "/dist/tdnext");
        assert_eq!(
            entry.get("shortcuts").and_then(serde_json::Value::as_str),
            Some("")
        );
        assert_eq!(shortcuts_for("shell"), "Shell keys   Ctrl+g Zellij lock");
        Ok(())
    }

    #[test]
    fn unknown_icons_fall_back_to_brackets() -> Result<()> {
        let entry = view_entry(&view("app"), "[]", "/dist/tdnext");
        assert_eq!(
            entry.get("icon").and_then(serde_json::Value::as_str),
            Some("[]")
        );
        Ok(())
    }

    #[test]
    fn missing_icons_default_to_brackets_in_config() -> Result<()> {
        let config = build_config(&[view("app")], &HashMap::new(), "/dist/tdnext");
        let icon = config
            .get("views")
            .and_then(|views| views.get(0))
            .and_then(|entry| entry.get("icon"))
            .and_then(serde_json::Value::as_str);
        assert_eq!(icon, Some("[]"));
        assert_eq!(
            config
                .get("views")
                .map(|views| views.as_array().map(Vec::len)),
            Some(Some(1))
        );
        Ok(())
    }

    #[test]
    fn codicons_require_a_selected_and_enabled_provider() -> Result<()> {
        let selected = Appearance {
            provider_id: Some("icons".to_string()),
            codicons: true,
        };
        assert!(use_codicons(
            &selected,
            &BTreeSet::from(["icons".to_string()])
        ));
        assert!(!use_codicons(&selected, &BTreeSet::new()));
        let text = Appearance {
            provider_id: None,
            codicons: true,
        };
        assert!(!use_codicons(&text, &BTreeSet::from(["icons".to_string()])));
        Ok(())
    }

    #[test]
    fn session_icons_follow_provider_selection_and_manifest_fallback() {
        let mut files = view("files");
        files.icon = Some("▤".to_string());
        files.icon_key = Some("files".to_string());
        let mut custom = view("custom");
        custom.icon = Some("X".to_string());
        let mut unknown = view("unknown");
        unknown.icon = Some("Y".to_string());
        unknown.icon_key = Some("custom.unknown".to_string());
        let views = [files, custom, unknown];
        for (provider, enabled, codicons, expected) in [
            (None, true, false, ["▤", "X", "Y"]),
            (Some("icons"), false, true, ["▤", "X", "Y"]),
            (Some("icons"), true, false, ["[F]", "X", "[]"]),
            (Some("icons"), true, true, ["\u{ea83}", "X", "[]"]),
        ] {
            let appearance = Appearance {
                provider_id: provider.map(str::to_string),
                codicons,
            };
            let enabled = if enabled {
                BTreeSet::from(["icons".to_string()])
            } else {
                BTreeSet::new()
            };
            let icons = session_icons(&views, &appearance, &enabled);
            for (id, expected) in ["files", "custom", "unknown"].into_iter().zip(expected) {
                assert_eq!(icons.get(id).map(String::as_str), Some(expected), "{id}");
            }
        }
    }
}
