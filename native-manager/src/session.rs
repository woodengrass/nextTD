//! Session membership: which enabled plugins form one desktop launch.
//!
//! Collection rejects an enabled id with no usable manifest instead of
//! silently skipping it; an explicit empty desktop (host only) is allowed,
//! matching the explicit-plugin-startup decision.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use tdnext_manager::Manifest;

use crate::run::ViewSpec;

/// Enabled views plus the host and protection set for one session.
pub struct Session {
    pub views: Vec<ViewSpec>,
    pub host_id: String,
    pub protected: BTreeSet<String>,
}

impl Session {
    /// Collect the session from the catalog: enabled views, the single
    /// compatible host, and the legacy protection set (settings plus the
    /// enabled host, never the whole enabled view set).
    pub fn collect(
        manifests: &BTreeMap<String, Manifest>,
        errors: &BTreeMap<String, String>,
        enabled: &BTreeSet<String>,
    ) -> Result<Self> {
        for manifest in manifests.values().filter(|m| enabled.contains(&m.id)) {
            let closure = tdnext_manager::graph::dependency_order(manifests, &manifest.id, errors)
                .map_err(|error| anyhow::anyhow!("{}: {error}", manifest.id))?;
            for dependency in closure {
                if !enabled.contains(&dependency) {
                    anyhow::bail!(
                        "Enabled plugin {} requires disabled plugin {dependency}",
                        manifest.id
                    );
                }
            }
        }
        for id in enabled {
            if !manifests.contains_key(id) {
                if let Some(diagnostic) = errors.get(id) {
                    anyhow::bail!("{diagnostic}");
                }
                anyhow::bail!("Required plugin not found: {id}");
            }
        }
        let views: Vec<ViewSpec> = manifests
            .values()
            .filter(|manifest| manifest.role() == "desktop.view" && enabled.contains(&manifest.id))
            .map(ViewSpec::of)
            .collect();
        let hosts: BTreeMap<String, Vec<String>> = manifests
            .values()
            .filter(|manifest| manifest.role() == "desktop.host" && enabled.contains(&manifest.id))
            .map(|manifest| (manifest.id.clone(), manifest.host_interfaces.clone()))
            .collect();
        let host_id = check_session(&hosts, &views)?;
        Ok(Self {
            views,
            protected: protected_plugins(&host_id),
            host_id,
        })
    }
}

/// Collect enabled views and the single host from already-owned data.
/// An explicit empty desktop is allowed: only the host is required.
pub fn check_session(hosts: &BTreeMap<String, Vec<String>>, views: &[ViewSpec]) -> Result<String> {
    if hosts.len() > 1 {
        let names: Vec<&str> = hosts.keys().map(String::as_str).collect();
        anyhow::bail!(
            "multiple desktop hosts enabled ({}); disable all but one and retry",
            names.join(", ")
        );
    }
    let (host_id, host_interfaces) = hosts
        .iter()
        .next()
        .map(|(id, interfaces)| (id.clone(), interfaces.clone()))
        .with_context(|| "no desktop host enabled; enable a host and retry")?;
    let mut incompatible: Vec<&str> = Vec::new();
    for view in views {
        if view
            .view_interfaces
            .iter()
            .any(|interface| !host_interfaces.contains(interface))
        {
            incompatible.push(view.id.as_str());
        }
    }
    if !incompatible.is_empty() {
        anyhow::bail!(
            "Host {host_id} is incompatible with views: {}",
            incompatible.join(", ")
        );
    }
    Ok(host_id)
}

/// Settings plus the enabled host stay protected; never all enabled views.
#[must_use]
pub fn protected_plugins(host_id: &str) -> BTreeSet<String> {
    BTreeSet::from(["settings".to_string(), host_id.to_string()])
}

#[cfg(test)]
mod tests {
    use super::{Session, check_session, protected_plugins};
    use crate::run::ViewSpec;
    use anyhow::Result;
    use std::collections::{BTreeMap, BTreeSet};

    const TERMINAL_VIEW: &str = "tdnext.terminal-view.v1";

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
            view_interfaces: vec![TERMINAL_VIEW.to_string()],
        }
    }

    fn hosts(ids: &[&str]) -> BTreeMap<String, Vec<String>> {
        ids.iter()
            .map(|id| ((*id).to_string(), vec![TERMINAL_VIEW.to_string()]))
            .collect()
    }

    fn expect_err<T>(result: Result<T>) -> Result<anyhow::Error> {
        match result {
            Ok(_) => anyhow::bail!("expected an error"),
            Err(error) => Ok(error),
        }
    }

    #[test]
    fn host_only_session_is_allowed() -> Result<()> {
        assert_eq!(check_session(&hosts(&["desktop"]), &[])?, "desktop");
        Ok(())
    }

    #[test]
    fn host_required_even_without_views() -> Result<()> {
        let error = expect_err(check_session(&BTreeMap::new(), &[]))?;
        assert!(error.to_string().contains("no desktop host"), "{error:?}");
        Ok(())
    }

    #[test]
    fn missing_host_is_a_useful_error() -> Result<()> {
        let error = expect_err(check_session(&BTreeMap::new(), &[view("files")]))?;
        assert!(error.to_string().contains("no desktop host"), "{error:?}");
        Ok(())
    }

    #[test]
    fn second_host_must_be_disabled_first() -> Result<()> {
        let error = expect_err(check_session(
            &hosts(&["desktop", "other"]),
            &[view("files")],
        ))?;
        assert!(
            error.to_string().contains("multiple desktop hosts"),
            "{error:?}"
        );
        assert!(error.to_string().contains("desktop, other"), "{error:?}");
        Ok(())
    }

    #[test]
    fn incompatible_view_names_the_host() -> Result<()> {
        let mut strict = view("strict");
        strict.view_interfaces = vec!["other.host.view.v1".to_string()];
        let error = expect_err(check_session(&hosts(&["desktop"]), &[strict]))?;
        assert!(
            error
                .to_string()
                .contains("incompatible with views: strict"),
            "{error:?}"
        );
        Ok(())
    }

    #[test]
    fn compatible_session_returns_the_host() -> Result<()> {
        assert_eq!(
            check_session(&hosts(&["desktop"]), &[view("files")])?,
            "desktop"
        );
        Ok(())
    }

    #[test]
    fn protection_is_settings_plus_host_not_all_views() -> Result<()> {
        let protected = protected_plugins("desktop");
        assert_eq!(
            protected,
            BTreeSet::from(["desktop".to_string(), "settings".to_string()])
        );
        assert!(!protected.contains("files"));
        Ok(())
    }

    #[test]
    fn collect_rejects_enabled_plugin_without_manifest() -> Result<()> {
        let manager = tdnext_manager::Manager::at(tempfile::tempdir()?.path().to_path_buf())?;
        let enabled: BTreeSet<String> = ["desktop".to_string(), "ghost".to_string()]
            .into_iter()
            .collect();
        let error = expect_err(Session::collect(
            &manager.manifests,
            &manager.errors,
            &enabled,
        ))?;
        assert_eq!(error.to_string(), "Required plugin not found: ghost");
        Ok(())
    }

    #[test]
    fn collect_reports_legacy_python_diagnostic() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let plugin = dir.path().join("plugins").join("legacy");
        std::fs::create_dir_all(&plugin)?;
        std::fs::write(
            plugin.join("plugin.toml"),
            "id = \"legacy\"\nmanifest_version = 1\napi_version = 1\nname = \"Legacy\"\nversion = \"0.1.0\"\n\
             description = \"Legacy\"\ncapabilities = [\"desktop.view\"]\n\
             view_interfaces = [\"tdnext.terminal-view.v1\"]\ncommand = \"tdnext-legacy\"\n\
             entrypoint = \"tdnext.plugins.legacy.plugin:LegacyPlugin\"\n",
        )?;
        let manager = tdnext_manager::Manager::at(dir.path().to_path_buf())?;
        assert!(
            manager.errors.contains_key("legacy"),
            "{:?}",
            manager.errors
        );
        let enabled: BTreeSet<String> = ["desktop".to_string(), "legacy".to_string()]
            .into_iter()
            .collect();
        let error = expect_err(Session::collect(
            &manager.manifests,
            &manager.errors,
            &enabled,
        ))?;
        assert!(error.to_string().contains("legacy Python"), "{error:?}");
        Ok(())
    }

    #[test]
    fn collect_accepts_host_only_state() -> Result<()> {
        let manager = tdnext_manager::Manager::at(tempfile::tempdir()?.path().to_path_buf())?;
        let enabled: BTreeSet<String> = ["desktop".to_string()].into_iter().collect();
        let session = Session::collect(&manager.manifests, &manager.errors, &enabled)?;
        assert_eq!(session.host_id, "desktop");
        assert!(session.views.is_empty());
        Ok(())
    }
}
