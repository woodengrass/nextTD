//! Native plugin catalog: strict manifest parsing and directory discovery.
//!
//! A native manifest (`plugin.toml`) declares an executable program, never a
//! Python import path. Anything carrying `entrypoint` or `dependency_hook`
//! is a legacy Python plugin: it is reported in the errors map and never
//! enters the usable catalog, so persisted state stays recoverable.

use std::collections::BTreeMap;
use std::path::Path;

use anyhow::{Context, Result};
use serde::Deserialize;

pub const ROLE_HOST: &str = "desktop.host";
pub const ROLE_VIEW: &str = "desktop.view";
pub const ROLE_ICONS: &str = "icons.provider";

/// Native terminal interface every view speaks and the host provides.
pub const TERMINAL_VIEW: &str = "tdnext.terminal-view.v1";

/// Structural role of a manifest; the [`Manifest::role`] string form is canonical.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Host,
    View,
    Icons,
}

/// One trusted native plugin: an independent executable program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub id: String,
    pub name: String,
    pub description: String,
    pub version: String,
    pub placement: String,
    pub command: Option<String>,
    pub args: Vec<String>,
    pub icon: Option<String>,
    pub icon_key: Option<String>,
    pub requires_plugins: Vec<String>,
    pub requires_executables: Vec<String>,
    pub host_interfaces: Vec<String>,
    pub view_interfaces: Vec<String>,
    role: Role,
}

impl Manifest {
    /// Canonical structural role: `desktop.host`, `desktop.view`, or `icons.provider`.
    #[must_use]
    pub fn role(&self) -> &'static str {
        match self.role {
            Role::Host => ROLE_HOST,
            Role::View => ROLE_VIEW,
            Role::Icons => ROLE_ICONS,
        }
    }

    #[cfg(test)]
    pub(crate) fn test_manifest(
        id: &str,
        role: Role,
        dependencies: &[&str],
        executables: &[&str],
    ) -> Self {
        let (command, view_interfaces, host_interfaces) = match role {
            Role::Host => (None, Vec::new(), vec![TERMINAL_VIEW.to_string()]),
            Role::View => (
                Some(format!("tdnext-{id}")),
                vec![TERMINAL_VIEW.to_string()],
                Vec::new(),
            ),
            Role::Icons => (None, Vec::new(), Vec::new()),
        };
        Self {
            id: id.to_string(),
            name: id.to_string(),
            description: id.to_string(),
            version: "0.1.0".to_string(),
            placement: "sidebar".to_string(),
            command,
            args: Vec::new(),
            icon: None,
            icon_key: None,
            requires_plugins: dependencies.iter().map(ToString::to_string).collect(),
            requires_executables: executables.iter().map(ToString::to_string).collect(),
            host_interfaces,
            view_interfaces,
            role,
        }
    }
}

/// Raw on-disk shape. `entrypoint`/`dependency_hook` are accepted here only so
/// they can be diagnosed as legacy Python instead of a bare TOML error.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawManifest {
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    placement: Option<String>,
    #[serde(default)]
    command: Option<String>,
    #[serde(default)]
    args: Option<Vec<String>>,
    #[serde(default)]
    icon: Option<String>,
    #[serde(default)]
    icon_key: Option<String>,
    #[serde(default)]
    capabilities: Option<Vec<String>>,
    #[serde(default)]
    requires_plugins: Option<Vec<String>>,
    #[serde(default)]
    requires_executables: Option<Vec<String>>,
    #[serde(default)]
    manifest_version: Option<i64>,
    #[serde(default)]
    api_version: Option<i64>,
    #[serde(default)]
    host_interfaces: Option<Vec<String>>,
    #[serde(default)]
    view_interfaces: Option<Vec<String>>,
    #[serde(default)]
    entrypoint: Option<toml::Value>,
    #[serde(default)]
    dependency_hook: Option<toml::Value>,
}

fn is_id(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .next()
            .is_some_and(|first| first.is_ascii_lowercase())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-'
        })
}

fn is_icon_key(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .next()
            .is_some_and(|first| first.is_ascii_lowercase())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-' | b'.')
        })
}

fn is_interface(value: &str) -> bool {
    let mut parts = value.split('.');
    let Some(first) = parts.next() else {
        return false;
    };
    if !is_id(first) {
        return false;
    }
    let mut rest = 0;
    for part in parts {
        rest += 1;
        if part.is_empty()
            || !part
                .bytes()
                .next()
                .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            || !part.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-'
            })
        {
            return false;
        }
    }
    rest >= 1
}

fn is_capability(value: &str) -> bool {
    let mut parts = value.split('.');
    let Some(first) = parts.next() else {
        return false;
    };
    if !is_id(first) {
        return false;
    }
    for part in parts {
        if part.is_empty() || !is_id(part) {
            return false;
        }
    }
    value.contains('.')
}

fn is_terminal_icon(icon: &str) -> bool {
    let count = icon.chars().count();
    (1..=4).contains(&count)
        && icon.chars().all(|char| {
            ('!'..='~').contains(&char)
                || ('\u{2500}'..='\u{259f}').contains(&char)
                || matches!(char, '▤' | '▥')
        })
}

fn has_control(value: &str) -> bool {
    value.chars().any(char::is_control)
}

/// A rejected manifest: the usable `id` when one was declared, plus the diagnostic.
struct Invalid {
    id: Option<String>,
    diagnostic: String,
}

/// Validate one parsed manifest; the `Err` value is the catalog diagnostic.
fn validate(path: &Path, raw: RawManifest) -> Result<Manifest, Invalid> {
    if raw.entrypoint.is_some() || raw.dependency_hook.is_some() {
        let id = raw.id.clone().unwrap_or_else(|| path.display().to_string());
        return Err(Invalid {
            id: Some(id.clone()),
            diagnostic: format!(
                "Invalid plugin manifest {path}: legacy Python plugin '{id}' declares a Python \
                 entry point and is not supported by the native runtime",
                path = path.display()
            ),
        });
    }
    if raw.entrypoint.is_some() || raw.dependency_hook.is_some() {
        let id = raw.id.clone().unwrap_or_else(|| path.display().to_string());
        return Err(Invalid {
            id: Some(id.clone()),
            diagnostic: format!(
                "Invalid plugin manifest {path}: legacy Python plugin '{id}' declares a Python \
                 entry point and is not supported by the native runtime",
                path = path.display()
            ),
        });
    }
    let declared = raw.id.clone();
    let id = declared
        .clone()
        .filter(|id| is_id(id))
        .ok_or_else(|| Invalid {
            id: declared.clone(),
            diagnostic: format!(
                "Invalid plugin manifest {}: id must match [a-z][a-z0-9_-]*",
                path.display()
            ),
        })?;
    let invalid = |reason: &str| Invalid {
        id: Some(id.clone()),
        diagnostic: format!(
            "Invalid plugin manifest {} ({id}): {reason}",
            path.display()
        ),
    };
    if raw.manifest_version != Some(1) {
        return Err(invalid("manifest_version must be 1"));
    }
    if raw.api_version != Some(1) {
        return Err(invalid("api_version must be 1"));
    }
    let capabilities = raw
        .capabilities
        .ok_or_else(|| invalid("capabilities are required"))?;
    if capabilities.is_empty() || {
        let mut seen = std::collections::BTreeSet::new();
        !capabilities
            .iter()
            .all(|capability| seen.insert(capability))
    } {
        return Err(invalid("capabilities must be nonempty and unique"));
    }
    for capability in &capabilities {
        if !is_capability(capability) {
            return Err(invalid(&format!("invalid capability: {capability}")));
        }
        if (capability.starts_with("desktop.") || capability.starts_with("icons."))
            && *capability != ROLE_HOST
            && *capability != ROLE_VIEW
            && *capability != ROLE_ICONS
        {
            return Err(invalid(&format!("unknown structural role: {capability}")));
        }
    }
    let roles = [ROLE_HOST, ROLE_VIEW, ROLE_ICONS]
        .into_iter()
        .filter(|role| capabilities.iter().any(|capability| capability == role))
        .count();
    if roles != 1 {
        return Err(invalid(
            "capabilities must declare exactly one structural role",
        ));
    }
    let role = if capabilities
        .iter()
        .any(|capability| capability == ROLE_HOST)
    {
        Role::Host
    } else if capabilities
        .iter()
        .any(|capability| capability == ROLE_VIEW)
    {
        Role::View
    } else {
        Role::Icons
    };
    let name = raw
        .name
        .filter(|name| !name.is_empty())
        .ok_or_else(|| invalid("name must not be empty"))?;
    let description = raw
        .description
        .filter(|description| !description.is_empty())
        .ok_or_else(|| invalid("description must not be empty"))?;
    let version = raw
        .version
        .filter(|version| !version.is_empty())
        .ok_or_else(|| invalid("version must not be empty"))?;
    let placement = raw.placement.unwrap_or_else(|| "sidebar".to_string());
    if placement != "sidebar" && placement != "header" {
        return Err(invalid("placement must be sidebar or header"));
    }
    let command = match raw.command {
        Some(command) if command.is_empty() || has_control(&command) => {
            return Err(invalid(
                "command must be a non-empty executable without control characters",
            ));
        }
        Some(_) if role != Role::View => {
            return Err(invalid("only views may declare command"));
        }
        command => command,
    };
    if role == Role::View && command.is_none() {
        return Err(invalid("views must declare an executable command"));
    }
    let args = raw.args.unwrap_or_default();
    if args.iter().any(|arg| has_control(arg)) {
        return Err(invalid("args cannot contain control characters"));
    }
    let icon = match raw.icon {
        Some(icon) if !is_terminal_icon(&icon) => {
            return Err(invalid(
                "icon must use 1-4 printable ASCII or standard terminal drawing glyphs",
            ));
        }
        icon => icon,
    };
    if let Some(key) = &raw.icon_key
        && !is_icon_key(key)
    {
        return Err(invalid("icon_key must match [a-z][a-z0-9_.-]*"));
    }
    let host_interfaces = raw.host_interfaces.unwrap_or_default();
    let view_interfaces = raw.view_interfaces.unwrap_or_default();
    for interface in host_interfaces.iter().chain(view_interfaces.iter()) {
        if !is_interface(interface) {
            return Err(invalid(&format!("invalid interface: {interface}")));
        }
    }
    if host_interfaces.len()
        != host_interfaces
            .iter()
            .collect::<std::collections::BTreeSet<_>>()
            .len()
        || view_interfaces.len()
            != view_interfaces
                .iter()
                .collect::<std::collections::BTreeSet<_>>()
                .len()
    {
        return Err(invalid("interfaces must be unique"));
    }
    match role {
        Role::Host => {
            if host_interfaces.is_empty() {
                return Err(invalid("host_interfaces must declare supported interfaces"));
            }
            if !view_interfaces.is_empty() {
                return Err(invalid("only views may declare view_interfaces"));
            }
        }
        Role::View => {
            if view_interfaces.is_empty() {
                return Err(invalid("view_interfaces must declare required interfaces"));
            }
            if !host_interfaces.is_empty() {
                return Err(invalid("only hosts may declare host_interfaces"));
            }
        }
        Role::Icons => {
            if !host_interfaces.is_empty() || !view_interfaces.is_empty() {
                return Err(invalid(
                    "icons providers must not declare host or view interfaces",
                ));
            }
        }
    }
    Ok(Manifest {
        id,
        name,
        description,
        version,
        placement,
        command,
        args,
        icon,
        icon_key: raw.icon_key,
        requires_plugins: raw.requires_plugins.unwrap_or_default(),
        requires_executables: raw.requires_executables.unwrap_or_default(),
        host_interfaces,
        view_interfaces,
        role,
    })
}

/// Compiled-in native manifests for the six real plugin ids.
///
/// They travel the same strict [`validate`] path as custom manifests, so any
/// drift from the contract fails loudly instead of shipping a broken builtin.
pub(crate) fn builtin_manifests() -> Result<BTreeMap<String, Manifest>> {
    const BUILTINS: &[&str] = &[
        r#"
id = "desktop"
manifest_version = 1
api_version = 1
name = "Desktop"
version = "0.1.0"
description = "Persistent terminal desktop shell"
capabilities = ["desktop.host"]
host_interfaces = ["tdnext.terminal-view.v1"]
"#,
        r#"
id = "files"
manifest_version = 1
api_version = 1
name = "Files"
version = "0.1.0"
description = "Browse and manage local files"
capabilities = ["desktop.view", "files.browse"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "tdnext-files"
icon = "▤"
icon_key = "files"
"#,
        r#"
id = "shell"
manifest_version = 1
api_version = 1
name = "Shell"
version = "0.1.0"
description = "Run an interactive shell inside the desktop"
capabilities = ["desktop.view", "shell.interactive"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "/bin/sh"
icon = ">_"
icon_key = "shell"
"#,
        r#"
id = "settings"
manifest_version = 1
api_version = 1
name = "Settings"
version = "0.1.0"
description = "Manage installed and enabled plugins"
placement = "header"
capabilities = ["desktop.view", "settings.manage"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "tdnext-settings"
icon_key = "settings"
"#,
        r#"
id = "monitor"
manifest_version = 1
api_version = 1
name = "Monitor"
version = "0.1.0"
description = "Launch the real btop system monitor"
capabilities = ["desktop.view", "system.monitor"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "btop"
icon = "▥"
icon_key = "monitor"
requires_executables = ["btop"]
"#,
        r#"
id = "icons"
manifest_version = 1
api_version = 1
name = "Codicons"
version = "0.1.0"
description = "Optional compatibility text and Nerd Font Codicons service"
capabilities = ["icons.provider"]
"#,
    ];
    let mut manifests = BTreeMap::new();
    for doc in BUILTINS {
        let raw: RawManifest =
            toml::from_str(doc).with_context(|| "Builtin manifest does not parse")?;
        match validate(Path::new("<builtin>"), raw) {
            Ok(manifest) => {
                manifests.insert(manifest.id.clone(), manifest);
            }
            Err(rejected) => {
                anyhow::bail!("Invalid builtin manifest: {}", rejected.diagnostic);
            }
        }
    }
    Ok(manifests)
}

/// Resolve a relative program path against the manifest's own directory, so a
/// custom view keeps working no matter where the manager runs from. Bare
/// names stay bare for `PATH` lookup; the TOML schema is unchanged.
fn absolutize_command(path: &Path, manifest: &mut Manifest) {
    let Some(command) = manifest.command.clone() else {
        return;
    };
    if command.contains('/')
        && let Some(dir) = path.parent()
    {
        manifest.command = Some(dir.join(&command).display().to_string());
    }
}

/// Discover native manifests under `<root>/*/plugin.toml`.
///
/// Returns usable manifests plus diagnostics keyed by plugin id (or by path
/// when no id could be read). Duplicate ids invalidate every claimant.
pub fn discover(roots: &[&Path]) -> (BTreeMap<String, Manifest>, BTreeMap<String, String>) {
    let mut manifests: BTreeMap<String, Manifest> = BTreeMap::new();
    let mut errors: BTreeMap<String, String> = BTreeMap::new();
    let mut claims: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for root in roots {
        let entries = std::fs::read_dir(root).map_or_else(
            |_| Vec::new(),
            |entries| {
                let mut paths: Vec<std::path::PathBuf> = entries
                    .filter_map(Result::ok)
                    .map(|entry| entry.path())
                    .filter(|path| path.is_dir())
                    .map(|path| path.join("plugin.toml"))
                    .filter(|path| path.is_file())
                    .collect();
                paths.sort();
                paths
            },
        );
        for path in entries {
            let invalid_text = std::fs::read_to_string(&path)
                .map_err(|error| format!("Invalid plugin manifest {}: {error}", path.display()));
            let text = match invalid_text {
                Err(diagnostic) => {
                    errors.insert(path.display().to_string(), diagnostic);
                    continue;
                }
                Ok(text) => text,
            };
            let raw: Result<RawManifest, String> = toml::from_str(&text)
                .map_err(|error| format!("Invalid plugin manifest {}: {error}", path.display()));
            let raw = match raw {
                Err(diagnostic) => {
                    errors.insert(path.display().to_string(), diagnostic);
                    continue;
                }
                Ok(raw) => raw,
            };
            if let Some(declared) = raw.id.clone() {
                claims
                    .entry(declared)
                    .or_default()
                    .push(path.display().to_string());
            }
            match validate(&path, raw) {
                Ok(mut manifest) => {
                    absolutize_command(&path, &mut manifest);
                    manifests.insert(manifest.id.clone(), manifest);
                }
                Err(rejected) => {
                    errors.insert(
                        rejected.id.unwrap_or_else(|| path.display().to_string()),
                        rejected.diagnostic,
                    );
                }
            }
        }
    }
    for (identity, paths) in &claims {
        if paths.len() > 1 {
            manifests.remove(identity);
            errors.insert(
                identity.clone(),
                format!(
                    "Duplicate plugin id {identity}: {}; remove the conflict",
                    paths.join(", ")
                ),
            );
        } else if errors.contains_key(identity) {
            manifests.remove(identity);
        }
    }
    (manifests, errors)
}

#[cfg(test)]
mod tests {
    use super::{discover, validate};
    use anyhow::{Context, Result};
    use std::path::Path;

    fn dir_with(manifests: &[(&str, &str)]) -> Result<tempfile::TempDir> {
        let dir = tempfile::tempdir()?;
        for (id, body) in manifests {
            let plugin = dir.path().join(id);
            std::fs::create_dir(&plugin)?;
            std::fs::write(plugin.join("plugin.toml"), body)?;
        }
        Ok(dir)
    }

    fn diagnostic_for(manifests: &[(&str, &str)], id: &str) -> Result<String> {
        let dir = dir_with(manifests)?;
        let (found, errors) = discover(&[dir.path()]);
        assert!(found.is_empty(), "{found:?}");
        errors
            .get(id)
            .cloned()
            .with_context(|| format!("no diagnostic for {id}: {errors:?}"))
    }

    const VIEW: &str = r#"
id = "app"
manifest_version = 1
api_version = 1
name = "App"
version = "0.1.0"
description = "An app"
capabilities = ["desktop.view", "app.main"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "tdnext-app"
"#;

    #[test]
    fn valid_native_view_is_discovered() -> Result<()> {
        let dir = dir_with(&[("app", VIEW)])?;
        let (manifests, errors) = discover(&[dir.path()]);
        assert!(errors.is_empty(), "{errors:?}");
        let manifest = &manifests["app"];
        assert_eq!(manifest.role(), "desktop.view");
        assert_eq!(manifest.command.as_deref(), Some("tdnext-app"));
        Ok(())
    }

    #[test]
    fn legacy_python_entrypoint_is_invalid_but_recoverable() -> Result<()> {
        let diagnostic = diagnostic_for(
            &[(
                "app",
                r#"
id = "app"
manifest_version = 1
api_version = 1
name = "App"
version = "0.1.0"
description = "Legacy"
capabilities = ["desktop.view"]
view_interfaces = ["tdnext.textual-view.v1"]
entrypoint = "tdnext.plugins.app.plugin:AppPlugin"
"#,
            )],
            "app",
        )?;
        assert!(diagnostic.contains("legacy Python"), "{diagnostic}");
        Ok(())
    }

    #[test]
    fn legacy_dependency_hook_is_invalid() -> Result<()> {
        let diagnostic = diagnostic_for(
            &[(
                "monitor",
                r#"
id = "monitor"
manifest_version = 1
api_version = 1
name = "Monitor"
version = "0.1.0"
description = "Legacy"
capabilities = ["desktop.view"]
view_interfaces = ["tdnext.textual-view.v1"]
command = "btop"
dependency_hook = "tdnext.plugins.monitor.dependencies:MonitorDependencies"
"#,
            )],
            "monitor",
        )?;
        assert!(diagnostic.contains("legacy Python"), "{diagnostic}");
        Ok(())
    }

    #[test]
    fn view_without_command_is_invalid() -> Result<()> {
        let body = VIEW.replace("command = \"tdnext-app\"\n", "");
        let diagnostic = diagnostic_for(&[("app", &body)], "app")?;
        assert!(diagnostic.contains("executable command"), "{diagnostic}");
        Ok(())
    }

    #[test]
    fn duplicate_ids_invalidate_every_claimant() -> Result<()> {
        let root = tempfile::tempdir()?;
        for name in ["a", "b"] {
            let plugin = root.path().join(name);
            std::fs::create_dir(&plugin)?;
            std::fs::write(plugin.join("plugin.toml"), VIEW)?;
        }
        let (manifests, errors) = discover(&[root.path()]);
        assert!(manifests.is_empty());
        assert!(errors["app"].contains("Duplicate plugin id"));
        Ok(())
    }

    #[test]
    fn host_without_interfaces_is_invalid() -> Result<()> {
        let diagnostic = diagnostic_for(
            &[(
                "desktop",
                r#"
id = "desktop"
manifest_version = 1
api_version = 1
name = "Desktop"
version = "0.1.0"
description = "Host"
capabilities = ["desktop.host"]
"#,
            )],
            "desktop",
        )?;
        assert!(diagnostic.contains("host_interfaces"), "{diagnostic}");
        Ok(())
    }

    #[test]
    fn host_must_not_declare_command() -> Result<()> {
        let diagnostic = diagnostic_for(
            &[(
                "desktop",
                r#"
id = "desktop"
manifest_version = 1
api_version = 1
name = "Desktop"
version = "0.1.0"
description = "Host"
capabilities = ["desktop.host"]
host_interfaces = ["tdnext.terminal-view.v1"]
command = "tdnext-desktop"
"#,
            )],
            "desktop",
        )?;
        assert!(
            diagnostic.contains("only views may declare command"),
            "{diagnostic}"
        );
        Ok(())
    }

    #[test]
    fn malformed_toml_keeps_path_key_and_other_plugins() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let bad = dir.path().join("bad");
        std::fs::create_dir(&bad)?;
        std::fs::write(bad.join("plugin.toml"), "{not toml")?;
        let good = dir.path().join("app");
        std::fs::create_dir(&good)?;
        std::fs::write(good.join("plugin.toml"), VIEW)?;
        let (manifests, errors) = discover(&[dir.path()]);
        assert!(manifests.contains_key("app"));
        assert_eq!(errors.len(), 1);
        let key = errors.keys().next().with_context(|| "expected one error")?;
        assert!(key.contains("bad"), "{key}");
        Ok(())
    }

    #[test]
    fn placement_and_icon_rules_hold() -> Result<()> {
        let icon = VIEW.replace("command = ", "icon = \"xx\"\ncommand = ");
        let raw: super::RawManifest =
            toml::from_str(&icon).map_err(|error| anyhow::anyhow!("{error}"))?;
        assert!(validate(Path::new("p"), raw).is_ok());
        let bad_icon = VIEW.replace("command = ", "icon = \"blast-off-!!\"\ncommand = ");
        let raw: super::RawManifest =
            toml::from_str(&bad_icon).map_err(|error| anyhow::anyhow!("{error}"))?;
        assert!(validate(Path::new("p"), raw).is_err());
        let bad_placement = VIEW.replace("command = ", "placement = \"footer\"\ncommand = ");
        let raw: super::RawManifest =
            toml::from_str(&bad_placement).map_err(|error| anyhow::anyhow!("{error}"))?;
        assert!(validate(Path::new("p"), raw).is_err());
        Ok(())
    }
}
