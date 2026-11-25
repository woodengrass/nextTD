//! Native plugin lifecycle manager: discovery, graph, compatibility,
//! install/enable/disable/uninstall, preservation, and diagnostics.
//!
//! Settings and the CLI share this contract. The installer track owns
//! `installer.rs`; the manager only calls its three functions to describe,
//! install, and verify external executables after explicit approval.

pub mod catalog;
pub mod graph;
pub mod installer;
pub mod state;
mod state_dir;

pub use catalog::Manifest;
pub use state::State;

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Result, bail};

use state::StateStore;

/// Lifecycle transition applied transactionally; persisted state is untouched
/// when the transition fails.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Install,
    Enable,
    Disable,
    Uninstall,
}

/// A confirmation no longer describes the cascade under the mutation lock.
#[derive(Debug)]
pub struct ChangedPreview(pub BTreeSet<String>);
impl std::fmt::Display for ChangedPreview {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Affected plugins changed; confirm the new list")
    }
}
impl std::error::Error for ChangedPreview {}

/// Catalog, state, and local binaries rooted at one state directory.
#[derive(Debug)]
pub struct Manager {
    pub root: PathBuf,
    pub manifests: BTreeMap<String, Manifest>,
    pub errors: BTreeMap<String, String>,
    store: StateStore,
    bin: PathBuf,
}

impl Manager {
    /// Load from the resolved state directory: `TDNEXT_STATE_DIR`, the
    /// checkout `state/` directory, or the XDG data location. Resolution
    /// never creates, migrates, or overwrites anything.
    pub fn load() -> Result<Self> {
        Self::at(state_dir::resolve_state_dir()?)
    }

    /// Load with an explicit state-directory root.
    pub fn at(root: PathBuf) -> Result<Self> {
        let root = if root.is_absolute() {
            root
        } else {
            std::env::current_dir()?.join(root)
        };
        let mut manifests = catalog::builtin_manifests()?;
        let mut errors = BTreeMap::new();
        let plugins_dir = root.join("plugins");
        let (custom, mut custom_errors) = catalog::discover(&[plugins_dir.as_path()]);
        for (id, manifest) in custom {
            match manifests.entry(id.clone()) {
                std::collections::btree_map::Entry::Vacant(slot) => {
                    slot.insert(manifest);
                }
                std::collections::btree_map::Entry::Occupied(_) => {
                    custom_errors.insert(
                        id.clone(),
                        format!(
                            "Duplicate plugin id {id}: shadows the builtin; remove the conflict"
                        ),
                    );
                }
            }
        }
        errors.append(&mut custom_errors);
        Ok(Self {
            store: StateStore::new(root.join("plugins.json")),
            bin: root.join("bin"),
            root,
            manifests,
            errors,
        })
    }

    /// Read the persisted installed/enabled snapshot.
    pub fn state(&self) -> Result<State> {
        self.store.read()
    }

    /// Resolve an executable by bare name: `state/bin` first, then the
    /// directory beside the running executable, then `PATH`. Absolute and
    /// relative paths containing a separator are checked directly.
    #[must_use]
    pub fn executable(&self, name: &str) -> Option<PathBuf> {
        if name.contains('/') {
            let path = PathBuf::from(name);
            return is_executable_file(&path).then_some(path);
        }
        if name.is_empty() {
            return None;
        }
        let local = self.bin.join(name);
        if is_executable_file(&local) {
            return Some(local);
        }
        if let Ok(current) = std::env::current_exe()
            && let Some(dir) = current.parent()
        {
            let sibling = dir.join(name);
            if is_executable_file(&sibling) {
                return Some(sibling);
            }
        }
        find_on_path(name)
    }

    /// Preview the enabled set a disable/uninstall of `id` would disturb,
    /// without changing anything.
    pub fn affected(&self, id: &str) -> Result<BTreeSet<String>> {
        let state = self.store.read()?;
        if !self.manifests.contains_key(id) && !state.installed.contains(id) {
            bail!("Plugin not found: {id}");
        }
        Ok(graph::dependants(&self.manifests, id, &state.enabled))
    }

    /// Apply one lifecycle transition. Takes the exclusive state lock first,
    /// so a second simultaneous operation fails fast with a busy error
    /// instead of interleaving. `protected` names plugins that must
    /// stay enabled; touching one fails the transition. `approve` is called
    /// with the installer consent text for each missing external executable;
    /// declining leaves state unchanged. Returns the affected ids in a
    /// deterministic order.
    pub fn apply(
        &self,
        action: Action,
        id: &str,
        protected: &BTreeSet<String>,
        approve: impl FnMut(&str) -> bool,
    ) -> Result<Vec<String>> {
        self.apply_previewed(action, id, protected, None, approve)
    }

    /// Refuse a changed preview before mutating the locked snapshot.
    pub fn apply_previewed(
        &self,
        action: Action,
        id: &str,
        protected: &BTreeSet<String>,
        preview: Option<&BTreeSet<String>>,
        mut approve: impl FnMut(&str) -> bool,
    ) -> Result<Vec<String>> {
        let _guard = self.store.lock()?;
        let state = self.store.read()?;
        match action {
            Action::Install | Action::Enable => {
                let order = if action == Action::Enable {
                    self.enable_order(id, &state)?
                } else {
                    graph::dependency_order(&self.manifests, id, &self.errors)?
                };
                let additions = order_set(&order);
                let installed = state.installed.union(&additions).cloned().collect();
                let enabled = if action == Action::Enable {
                    state.enabled.union(&additions).cloned().collect()
                } else {
                    state.enabled
                };
                let next = State::new(installed, enabled)?;
                self.store.check_writable()?;
                let tools = self.check_dependencies(&order, &mut approve)?;
                self.store.write(&next)?;
                for tool in tools {
                    tool.commit();
                }
                Ok(order)
            }
            Action::Disable | Action::Uninstall => {
                if !self.manifests.contains_key(id) && !state.installed.contains(id) {
                    bail!("Plugin not found: {id}");
                }
                let affected = graph::dependants(&self.manifests, id, &state.enabled);
                if preview.is_some_and(|expected| expected != &affected) {
                    return Err(ChangedPreview(affected).into());
                }
                self.guard_protected(&affected, protected)?;
                let mut installed = state.installed;
                if action == Action::Uninstall {
                    installed.remove(id);
                }
                let enabled: BTreeSet<String> =
                    state.enabled.difference(&affected).cloned().collect();
                self.store.write(&State::new(installed, enabled)?)?;
                Ok(affected.into_iter().collect())
            }
        }
    }

    /// Install missing external executables (with approval) and verify every
    /// required executable resolves, rejecting a broken btop replacement.
    fn check_dependencies(
        &self,
        order: &[String],
        approve: &mut impl FnMut(&str) -> bool,
    ) -> Result<Vec<installer::PendingTool>> {
        let mut downloads = Vec::new();
        let mut seen = BTreeSet::new();
        for name in order {
            let manifest = &self.manifests[name];
            for executable in &manifest.requires_executables {
                if !seen.insert(executable) {
                    continue;
                }
                if let Some(binary) = self.executable(executable) {
                    if executable == "btop" {
                        installer::check_btop(&binary)?;
                    }
                } else {
                    downloads.push((executable, installer::description(executable, &self.bin)?));
                }
            }
        }
        for (_, prompt) in &downloads {
            if !approve(prompt) {
                bail!("Installation declined; plugin state was not changed");
            }
        }
        downloads
            .into_iter()
            .map(|(executable, _)| installer::install(executable, &self.bin))
            .collect()
    }

    /// Dependency order for enabling `id`, selecting the single compatible
    /// host: the already-enabled one, the requested one, or the unique
    /// candidate compatible with every view.
    fn enable_order(&self, plugin_id: &str, state: &State) -> Result<Vec<String>> {
        let mut order = graph::dependency_order(&self.manifests, plugin_id, &self.errors)?;
        let mut hosts: BTreeMap<&str, &Manifest> = BTreeMap::new();
        for name in state.enabled.iter().chain(order.iter()) {
            if let Some(manifest) = self.manifests.get(name)
                && manifest.role() == catalog::ROLE_HOST
            {
                hosts.insert(manifest.id.as_str(), manifest);
            }
        }
        if hosts.len() > 1 {
            bail!("Exactly one desktop host may be enabled; disable the current host first");
        }
        let mut views: Vec<&Manifest> = state
            .enabled
            .iter()
            .chain(order.iter())
            .filter_map(|name| self.manifests.get(name))
            .filter(|manifest| manifest.role() == catalog::ROLE_VIEW)
            .collect();
        if !views.is_empty() && hosts.is_empty() {
            let candidates: Vec<&Manifest> = self
                .manifests
                .values()
                .filter(|manifest| {
                    manifest.role() == catalog::ROLE_HOST
                        && views.iter().all(|view| {
                            view.view_interfaces
                                .iter()
                                .all(|interface| manifest.host_interfaces.contains(interface))
                        })
                })
                .collect();
            if candidates.len() != 1 {
                let names = candidates
                    .iter()
                    .map(|manifest| manifest.id.as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                let names = if names.is_empty() {
                    "none".to_string()
                } else {
                    names
                };
                bail!("Select a compatible host explicitly; candidates: {names}");
            }
            let host_order =
                graph::dependency_order(&self.manifests, &candidates[0].id, &self.errors)?;
            let mut merged = host_order.clone();
            for name in &order {
                if !merged.contains(name) {
                    merged.push(name.clone());
                }
            }
            order = merged;
            for name in &host_order {
                if let Some(manifest) = self.manifests.get(name) {
                    if manifest.role() == catalog::ROLE_HOST {
                        hosts.insert(manifest.id.as_str(), manifest);
                    } else if manifest.role() == catalog::ROLE_VIEW {
                        views.push(manifest);
                    }
                }
            }
            if hosts.len() != 1 {
                bail!(
                    "Selected host dependencies require multiple hosts; fix the plugin dependencies"
                );
            }
        }
        if let Some(host) = hosts.values().next() {
            let incompatible: Vec<&str> = views
                .iter()
                .filter(|view| {
                    view.view_interfaces
                        .iter()
                        .any(|interface| !host.host_interfaces.contains(interface))
                })
                .map(|view| view.id.as_str())
                .collect();
            if !incompatible.is_empty() {
                bail!(
                    "Host {} is incompatible with views: {}",
                    host.id,
                    incompatible.join(", ")
                );
            }
        }
        Ok(order)
    }

    /// Refuse transitions that would disturb caller-protected plugins.
    fn guard_protected(
        &self,
        affected: &BTreeSet<String>,
        protected: &BTreeSet<String>,
    ) -> Result<()> {
        let blocked: Vec<&str> = affected
            .intersection(protected)
            .map(String::as_str)
            .collect();
        if blocked.is_empty() {
            Ok(())
        } else {
            bail!(
                "Refusing to disturb protected plugins: {}",
                blocked.join(", ")
            )
        }
    }
}

/// Union helper preserving the dependency order for the returned listing.
fn order_set(order: &[String]) -> BTreeSet<String> {
    order.iter().cloned().collect()
}

#[cfg(unix)]
fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt as _;
    path.is_file()
        && std::fs::metadata(path).is_ok_and(|metadata| metadata.permissions().mode() & 0o111 != 0)
}

#[cfg(not(unix))]
fn is_executable_file(path: &Path) -> bool {
    path.is_file()
}

fn find_on_path(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|dir| dir.join(name))
            .find(|candidate| is_executable_file(candidate))
    })
}

#[cfg(test)]
#[allow(clippy::too_many_lines)]
mod tests {
    use super::{Action, Manager};
    use crate::catalog::{Manifest, Role};
    use anyhow::{Context, Result};
    use std::collections::{BTreeMap, BTreeSet};
    use std::path::{Path, PathBuf};

    fn set(names: &[&str]) -> BTreeSet<String> {
        names.iter().map(ToString::to_string).collect()
    }

    fn programmatic(
        entries: &[(&str, Role, &[&str], &[&str])],
    ) -> Result<(tempfile::TempDir, Manager)> {
        let dir = tempfile::tempdir()?;
        let root = dir.path().to_path_buf();
        let mut manifests = BTreeMap::new();
        for (id, role, dependencies, executables) in entries {
            manifests.insert(
                (*id).to_string(),
                Manifest::test_manifest(id, *role, dependencies, executables),
            );
        }
        Ok((
            dir,
            Manager {
                root: root.clone(),
                manifests,
                errors: BTreeMap::new(),
                store: crate::state::StateStore::new(root.join("plugins.json")),
                bin: root.join("bin"),
            },
        ))
    }

    fn write_state(manager: &Manager, installed: &[&str], enabled: &[&str]) -> Result<()> {
        manager
            .store
            .write(&crate::state::State::new(set(installed), set(enabled))?)?;
        Ok(())
    }

    fn decline(_: &str) -> bool {
        false
    }

    #[test]
    fn builtins_match_real_manifests_on_native_interface() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let manager = Manager::at(dir.path().to_path_buf())?;
        assert!(manager.errors.is_empty(), "{:?}", manager.errors);
        for id in ["desktop", "files", "shell", "settings", "monitor", "icons"] {
            assert!(manager.manifests.contains_key(id), "missing builtin {id}");
        }
        assert_eq!(manager.manifests["desktop"].role(), "desktop.host");
        assert!(manager.manifests["desktop"].command.is_none());
        for id in ["files", "shell", "settings", "monitor"] {
            let manifest = &manager.manifests[id];
            assert_eq!(manifest.role(), "desktop.view");
            assert!(
                manifest
                    .view_interfaces
                    .contains(&"tdnext.terminal-view.v1".to_string()),
                "{id} must use the native terminal interface"
            );
            assert!(manifest.command.is_some(), "{id} must declare a command");
        }
        assert_eq!(manager.manifests["icons"].role(), "icons.provider");
        assert_eq!(manager.manifests["settings"].placement, "header");
        assert!(
            manager.manifests["monitor"]
                .requires_executables
                .contains(&"btop".to_string())
        );
        Ok(())
    }

    #[test]
    fn install_resolves_dependencies_without_enabling() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("base", Role::View, &[], &[]),
            ("app", Role::View, &["base"], &[]),
        ])?;
        let order = manager.apply(Action::Install, "app", &set(&[]), decline)?;
        assert_eq!(order, vec!["base".to_string(), "app".to_string()]);
        let state = manager.state()?;
        assert_eq!(state.installed, set(&["base", "app"]));
        assert!(state.enabled.is_empty());
        Ok(())
    }

    #[test]
    fn enable_installs_host_and_view_together() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("app", Role::View, &["desktop"], &[]),
        ])?;
        let order = manager.apply(Action::Enable, "app", &set(&[]), decline)?;
        assert_eq!(order, vec!["desktop".to_string(), "app".to_string()]);
        let state = manager.state()?;
        assert_eq!(state.installed, set(&["desktop", "app"]));
        assert_eq!(state.enabled, set(&["desktop", "app"]));
        Ok(())
    }

    #[test]
    fn enable_selects_the_unique_compatible_host() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("app", Role::View, &[], &[]),
        ])?;
        manager.apply(Action::Enable, "app", &set(&[]), decline)?;
        assert_eq!(manager.state()?.enabled, set(&["desktop", "app"]));
        Ok(())
    }

    #[test]
    fn enable_rejects_a_second_host_without_state_change() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("other", Role::Host, &[], &[]),
        ])?;
        manager.apply(Action::Enable, "desktop", &set(&[]), decline)?;
        let before = std::fs::read(manager.store.path.clone())?;
        match manager.apply(Action::Enable, "other", &set(&[]), decline) {
            Err(error) => assert!(
                error.to_string().contains("Exactly one desktop host"),
                "{error:?}"
            ),
            Ok(order) => panic!("second host accepted: {order:?}"),
        }
        assert_eq!(std::fs::read(manager.store.path.clone())?, before);
        Ok(())
    }

    #[test]
    fn enable_rejects_views_the_host_cannot_serve() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let plugins = dir.path().join("plugins").join("strict");
        std::fs::create_dir_all(&plugins)?;
        std::fs::write(
            plugins.join("plugin.toml"),
            r#"
id = "strict"
manifest_version = 1
api_version = 1
name = "Strict"
version = "0.1.0"
description = "Needs another host"
capabilities = ["desktop.view"]
view_interfaces = ["other.host.view.v1"]
command = "tdnext-strict"
"#,
        )?;
        let manager = Manager::at(dir.path().to_path_buf())?;
        manager.apply(Action::Enable, "desktop", &set(&[]), decline)?;
        match manager.apply(Action::Enable, "strict", &set(&[]), decline) {
            Err(error) => assert!(
                error.to_string().contains("incompatible with views"),
                "{error:?}"
            ),
            Ok(order) => panic!("incompatible view accepted: {order:?}"),
        }
        Ok(())
    }

    #[test]
    fn disable_cascades_through_enabled_dependants() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("base", Role::View, &[], &[]),
            ("mid", Role::View, &["base"], &[]),
            ("app", Role::View, &["mid"], &[]),
            ("other", Role::View, &[], &[]),
        ])?;
        write_state(
            &manager,
            &["base", "mid", "app", "other"],
            &["base", "mid", "app", "other"],
        )?;
        let affected = manager.apply(Action::Disable, "base", &set(&[]), decline)?;
        assert_eq!(
            affected,
            vec!["app".to_string(), "base".to_string(), "mid".to_string()]
        );
        assert_eq!(manager.state()?.enabled, set(&["other"]));
        Ok(())
    }

    #[test]
    fn uninstall_keeps_dependants_installed_and_reinstallable() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("app", Role::View, &["desktop"], &[]),
        ])?;
        write_state(&manager, &["desktop", "app"], &["desktop", "app"])?;
        let affected = manager.apply(Action::Uninstall, "desktop", &set(&[]), decline)?;
        assert_eq!(
            affected,
            set(&["desktop", "app"]).into_iter().collect::<Vec<_>>()
        );
        let state = manager.state()?;
        assert_eq!(state.installed, set(&["app"]));
        assert!(state.enabled.is_empty());
        manager.apply(Action::Install, "desktop", &set(&[]), decline)?;
        manager.apply(Action::Enable, "app", &set(&[]), decline)?;
        assert_eq!(manager.state()?.enabled, set(&["desktop", "app"]));
        Ok(())
    }

    #[test]
    fn uninstall_unknown_plugin_leaves_state_untouched() -> Result<()> {
        let (_dir, manager) = programmatic(&[("base", Role::View, &[], &[])])?;
        write_state(&manager, &["base"], &["base"])?;
        let before = std::fs::read(manager.store.path.clone())?;
        match manager.apply(Action::Uninstall, "missing", &set(&[]), decline) {
            Err(error) => assert!(error.to_string().contains("Plugin not found"), "{error:?}"),
            Ok(order) => panic!("unknown uninstall accepted: {order:?}"),
        }
        assert_eq!(std::fs::read(manager.store.path.clone())?, before);
        Ok(())
    }

    #[test]
    fn missing_executable_without_installer_fails_closed() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("app", Role::View, &[], &["tdnext-test-missing-executable"]),
        ])?;
        let mut approved = false;
        let result = manager.apply(Action::Enable, "app", &set(&[]), |_: &str| {
            approved = true;
            true
        });
        match result {
            Err(error) => assert!(
                error.to_string().contains("No local installer"),
                "{error:?}"
            ),
            Ok(order) => panic!("missing executable accepted: {order:?}"),
        }
        assert!(!approved, "no consent prompt for an uninstallable binary");
        assert_eq!(manager.state()?, crate::state::State::default());
        Ok(())
    }

    #[test]
    fn declined_consent_leaves_state_unchanged() -> Result<()> {
        let empty = tempfile::tempdir()?;
        with_path(
            empty.path(),
            "tests::declined_consent_leaves_state_unchanged",
        )?;
        if !is_child() {
            return Ok(());
        }
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("monitor", Role::View, &[], &["btop"]),
        ])?;
        assert!(
            manager.executable("btop").is_none(),
            "PATH isolation leaked a btop"
        );
        let mut prompted = Vec::new();
        let result = manager.apply(Action::Enable, "monitor", &set(&[]), |prompt: &str| {
            prompted.push(prompt.to_string());
            false
        });
        match result {
            Err(error) => assert!(error.to_string().contains("declined"), "{error:?}"),
            Ok(order) => panic!("declined install accepted: {order:?}"),
        }
        assert_eq!(prompted.len(), 1);
        assert!(prompted[0].contains("btop 1.4.8"), "{}", prompted[0]);
        assert_eq!(manager.state()?, crate::state::State::default());
        Ok(())
    }

    #[test]
    fn declining_second_tool_leaves_first_and_state_untouched() -> Result<()> {
        let empty = tempfile::tempdir()?;
        with_path(
            empty.path(),
            "tests::declining_second_tool_leaves_first_and_state_untouched",
        )?;
        if !is_child() {
            return Ok(());
        }
        let (_dir, manager) = programmatic(&[("app", Role::View, &[], &["btop", "test-tool"])])?;
        write_state(&manager, &[], &[])?;
        let before = std::fs::read(&manager.store.path)?;
        let result = manager.apply(Action::Install, "app", &set(&[]), |prompt| {
            !prompt.contains("test-tool")
        });
        assert!(
            matches!(result, Err(ref e) if e.to_string().contains("declined")),
            "{result:?}"
        );
        assert!(!manager.bin.join("btop").exists());
        assert_eq!(std::fs::read(&manager.store.path)?, before);
        Ok(())
    }

    #[test]
    fn unwritable_state_directory_prevents_download() -> Result<()> {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let empty = tempfile::tempdir()?;
        with_path(
            empty.path(),
            "tests::unwritable_state_directory_prevents_download",
        )?;
        if !is_child() {
            return Ok(());
        }
        let (root, manager) = programmatic(&[("app", Role::View, &[], &["btop"])])?;
        std::fs::create_dir(&manager.bin)?;
        let dir = root.path().join("settings");
        std::fs::create_dir(&dir)?;
        let target = dir.join("saved.json");
        std::fs::write(&target, b"{}")?;
        symlink(&target, &manager.store.path)?;
        // A previously created lock remains writable in a now read-only directory.
        drop(manager.store.lock()?);
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o555))?;
        let result = manager.apply(Action::Install, "app", &set(&[]), |_| true);
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
        assert!(
            matches!(result, Err(ref e) if format!("{e:#}").contains("Permission denied")),
            "{result:?}"
        );
        assert!(!manager.bin.join("btop").exists());
        assert_eq!(std::fs::read(target)?, b"{}");
        Ok(())
    }

    #[test]
    fn wrong_btop_binary_is_rejected_without_state_change() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("monitor", Role::View, &[], &["btop"]),
        ])?;
        std::fs::create_dir_all(&manager.bin)?;
        let impostor = manager.bin.join("btop");
        std::fs::write(&impostor, "#!/bin/sh\nprintf 'htop 1.0\\n'\n")?;
        set_executable(&impostor)?;
        warm_exec(&impostor)?;
        match manager.apply(Action::Enable, "monitor", &set(&[]), decline) {
            Err(error) => assert!(
                error.to_string().contains("not a working btop"),
                "{error:?}"
            ),
            Ok(order) => panic!("impostor btop accepted: {order:?}"),
        }
        assert_eq!(manager.state()?, crate::state::State::default());
        Ok(())
    }

    #[test]
    fn affected_previews_disable_without_mutating() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("base", Role::View, &[], &[]),
            ("app", Role::View, &["base"], &[]),
        ])?;
        write_state(&manager, &["base", "app"], &["base", "app"])?;
        let preview = manager.affected("base")?;
        assert_eq!(preview, set(&["base", "app"]));
        assert_eq!(manager.state()?.enabled, set(&["base", "app"]));
        Ok(())
    }

    #[test]
    fn protected_plugins_block_disable() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("app", Role::View, &["desktop"], &[]),
        ])?;
        write_state(&manager, &["desktop", "app"], &["desktop", "app"])?;
        let before = std::fs::read(manager.store.path.clone())?;
        match manager.apply(Action::Disable, "desktop", &set(&["desktop"]), decline) {
            Err(error) => assert!(error.to_string().contains("protected"), "{error:?}"),
            Ok(order) => panic!("protected disable accepted: {order:?}"),
        }
        assert_eq!(std::fs::read(manager.store.path.clone())?, before);
        Ok(())
    }

    #[test]
    fn executable_prefers_state_bin_over_path() -> Result<()> {
        let shadowed = tempfile::tempdir()?;
        std::fs::write(shadowed.path().join("tdnext-probe"), "#!/bin/sh\nexit 0\n")?;
        set_executable(&shadowed.path().join("tdnext-probe"))?;
        with_path(
            shadowed.path(),
            "tests::executable_prefers_state_bin_over_path",
        )?;
        if !is_child() {
            return Ok(());
        }
        let (_dir, manager) = programmatic(&[])?;
        std::fs::create_dir_all(&manager.bin)?;
        std::fs::write(manager.bin.join("tdnext-probe"), "#!/bin/sh\nexit 0\n")?;
        set_executable(&manager.bin.join("tdnext-probe"))?;
        let found = manager
            .executable("tdnext-probe")
            .with_context(|| "probe must resolve")?;
        assert_eq!(found, manager.bin.join("tdnext-probe"));
        assert!(
            manager
                .executable("tdnext-test-missing-executable")
                .is_none()
        );
        Ok(())
    }

    #[test]
    fn corrupt_state_is_surfaced_not_reset() -> Result<()> {
        let (_dir, manager) = programmatic(&[("base", Role::View, &[], &[])])?;
        std::fs::write(manager.store.path.clone(), "{not json")?;
        match manager.state() {
            Err(error) => assert!(
                error.to_string().contains("Cannot read plugin state"),
                "{error:?}"
            ),
            Ok(state) => panic!("corrupt state silently reset: {state:?}"),
        }
        Ok(())
    }

    #[test]
    fn custom_manifest_shadowing_a_builtin_is_invalid() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let plugins = dir.path().join("plugins").join("desktop");
        std::fs::create_dir_all(&plugins)?;
        std::fs::write(
            plugins.join("plugin.toml"),
            r#"
id = "desktop"
manifest_version = 1
api_version = 1
name = "Shadow"
version = "0.1.0"
description = "Tries to replace the host"
capabilities = ["desktop.host"]
host_interfaces = ["other.host.v1"]
"#,
        )?;
        let manager = Manager::at(dir.path().to_path_buf())?;
        assert!(
            manager.errors["desktop"].contains("Duplicate"),
            "{}",
            manager.errors["desktop"]
        );
        assert!(
            manager.manifests["desktop"]
                .host_interfaces
                .contains(&"tdnext.terminal-view.v1".to_string())
        );
        Ok(())
    }

    #[test]
    fn load_ignores_a_checkout_in_the_working_directory() -> Result<()> {
        const NAME: &str = "tests::load_ignores_a_checkout_in_the_working_directory";
        let fake = tempfile::tempdir()?;
        std::fs::write(
            fake.path().join("pyproject.toml"),
            "[project]\nname = \"terminal-desktop-next\"\n",
        )?;
        std::fs::create_dir_all(fake.path().join("src").join("tdnext"))?;
        spawn_self(NAME, |command| {
            command
                .current_dir(fake.path())
                .env_remove("TDNEXT_STATE_DIR");
        })?;
        if !is_child() {
            return Ok(());
        }
        let manager = Manager::load()?;
        assert_ne!(
            manager.root,
            fake.path().join("state"),
            "state followed the working directory"
        );
        Ok(())
    }

    #[test]
    fn concurrent_mutation_fails_fast_with_busy_error() -> Result<()> {
        let (_dir, manager) = programmatic(&[
            ("desktop", Role::Host, &[], &[]),
            ("app", Role::View, &["desktop"], &[]),
        ])?;
        let _guard = manager.store.lock()?;
        match manager.apply(Action::Enable, "app", &set(&[]), decline) {
            Err(error) => assert!(
                error.to_string().contains("locked by another operation"),
                "{error:?}"
            ),
            Ok(order) => panic!("concurrent mutation accepted: {order:?}"),
        }
        assert!(manager.state()?.enabled.is_empty());
        drop(_guard);
        manager.apply(Action::Enable, "app", &set(&[]), decline)?;
        assert_eq!(manager.state()?.enabled, set(&["desktop", "app"]));
        Ok(())
    }

    #[test]
    fn relative_command_resolves_against_manifest_directory() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let plugin = dir.path().join("plugins").join("local");
        std::fs::create_dir_all(plugin.join("bin"))?;
        std::fs::write(
            plugin.join("plugin.toml"),
            r#"
id = "local"
manifest_version = 1
api_version = 1
name = "Local"
version = "0.1.0"
description = "Local tool"
capabilities = ["desktop.view"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "bin/tool"
"#,
        )?;
        let tool = plugin.join("bin").join("tool");
        std::fs::write(&tool, "#!/bin/sh\nexit 0\n")?;
        set_executable(&tool)?;
        let manager = Manager::at(dir.path().to_path_buf())?;
        assert!(manager.errors.is_empty(), "{:?}", manager.errors);
        let command = manager.manifests["local"]
            .command
            .clone()
            .with_context(|| "command must survive discovery")?;
        assert_eq!(PathBuf::from(&command), tool);
        assert_eq!(manager.executable(&command), Some(tool));
        Ok(())
    }

    #[test]
    fn listing_never_writes() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let plugin = dir.path().join("plugins").join("local");
        std::fs::create_dir_all(&plugin)?;
        std::fs::write(
            plugin.join("plugin.toml"),
            r#"
id = "local"
manifest_version = 1
api_version = 1
name = "Local"
version = "0.1.0"
description = "Local tool"
capabilities = ["desktop.view"]
view_interfaces = ["tdnext.terminal-view.v1"]
command = "tdnext-local"
"#,
        )?;
        let manager = Manager::at(dir.path().to_path_buf())?;
        let _guard = manager.store.lock()?;
        manager.store.write(&crate::state::State::new(
            set(&["desktop", "local"]),
            set(&["desktop"]),
        )?)?;
        drop(_guard);
        let before = snapshot_tree(dir.path())?;
        let manager = Manager::at(dir.path().to_path_buf())?;
        let _ = manager.state()?;
        let _ = manager.affected("local")?;
        assert_eq!(snapshot_tree(dir.path())?, before);
        Ok(())
    }

    fn snapshot_tree(root: &Path) -> Result<Vec<(PathBuf, Vec<u8>)>> {
        let mut entries = Vec::new();
        let mut dirs = vec![root.to_path_buf()];
        while let Some(dir) = dirs.pop() {
            let mut paths: Vec<PathBuf> = std::fs::read_dir(&dir)?
                .map(|entry| entry.map(|entry| entry.path()))
                .collect::<std::io::Result<_>>()?;
            paths.sort();
            for path in paths {
                if path.is_dir() {
                    dirs.push(path);
                } else {
                    entries.push((path.clone(), std::fs::read(&path)?));
                }
            }
        }
        Ok(entries)
    }

    /// Run `test_name` in a child copy of this test binary. Process
    /// environments cannot be mutated soundly from a threaded test runner
    /// (`std::env::set_var` is `unsafe`, and this crate forbids `unsafe`),
    /// so environment control happens across a process boundary instead.
    /// `configure` adjusts the child's environment and working directory.
    fn spawn_self(
        test_name: &str,
        configure: impl FnOnce(&mut std::process::Command),
    ) -> Result<()> {
        if std::env::var_os("TDNEXT_MANAGER_TEST_CHILD").is_some() {
            return Ok(());
        }
        let executable = std::env::current_exe()?;
        let mut command = std::process::Command::new(executable);
        command
            .args(["--exact", test_name, "--nocapture"])
            .env("TDNEXT_MANAGER_TEST_CHILD", "1");
        configure(&mut command);
        let status = command.status()?;
        assert!(status.success(), "child test {test_name} failed");
        Ok(())
    }

    fn with_path(path: &Path, test_name: &str) -> Result<()> {
        spawn_self(test_name, |command| {
            command.env("PATH", path);
        })
    }

    fn is_child() -> bool {
        std::env::var_os("TDNEXT_MANAGER_TEST_CHILD").is_some()
    }

    #[cfg(unix)]
    fn set_executable(path: &PathBuf) -> Result<()> {
        use std::os::unix::fs::PermissionsExt as _;
        let mut permissions = std::fs::metadata(path)?.permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions)?;
        Ok(())
    }

    /// Run `program` once, absorbing a transient busy-file hold from the
    /// environment's file handling. Only setup is retried; the asserted
    /// behavior below stays a single deterministic check.
    fn warm_exec(program: &Path) -> Result<()> {
        for _ in 0..1000 {
            match std::process::Command::new(program)
                .arg("--version")
                .output()
            {
                Err(error) if error.raw_os_error() == Some(26) => {
                    std::thread::yield_now();
                }
                _ => return Ok(()),
            }
        }
        anyhow::bail!("test setup could not execute {}", program.display())
    }

    #[cfg(not(unix))]
    fn set_executable(_: &PathBuf) -> Result<()> {
        Ok(())
    }
}
