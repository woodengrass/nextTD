//! The `desktop [DIR]` subcommand: build a session config from enabled
//! state and launch the sibling desktop.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use tdnext_manager::Manager;

use crate::config::{build_config, session_icons};
use crate::icons::read_appearance;
use crate::launch::{Launch, launch};
use crate::session::Session;

fn sibling(current_exe: &Path, name: &str) -> Result<PathBuf> {
    let dir = current_exe
        .parent()
        .with_context(|| format!("cannot locate {name} next to {}", current_exe.display()))?;
    Ok(dir.join(name))
}

pub fn desktop(manager: &Manager, directory: &Path) -> Result<()> {
    let state = manager.state()?;
    let session = Session::collect(&manager.manifests, &manager.errors, &state.enabled)?;
    let appearance = read_appearance(&manager.root)?;
    let icons = session_icons(&session.views, &appearance, &state.enabled);
    let current_exe = std::env::current_exe().context("cannot locate the running tdnext binary")?;
    let desktop_bin = sibling(&current_exe, "tdnext-desktop")?;
    if !desktop_bin.is_file() {
        anyhow::bail!(
            "tdnext-desktop is missing next to {}; build the launcher and ship it beside tdnext",
            current_exe.display()
        );
    }
    let core_bin = sibling(&desktop_bin, "tdnext-core")?;
    if !core_bin.is_file() {
        anyhow::bail!(
            "tdnext-core is missing next to {}; build the patched core and ship it beside tdnext-desktop",
            desktop_bin.display()
        );
    }
    let directory = std::fs::canonicalize(directory)
        .with_context(|| format!("opening directory {}", directory.display()))?;
    if !directory.is_dir() {
        anyhow::bail!("{} is not a directory", directory.display());
    }
    let tdnext_exe = current_exe
        .to_str()
        .with_context(|| format!("launcher path {} is not UTF-8", current_exe.display()))?;
    let config = build_config(&session.views, &icons, tdnext_exe);
    use std::os::unix::fs::PermissionsExt as _;
    let temp = tempfile::Builder::new()
        .permissions(std::fs::Permissions::from_mode(0o700))
        .tempdir()
        .context("creating the session config directory")?;
    let config_path = temp.path().join("desktop.json");
    std::fs::write(&config_path, serde_json::to_string_pretty(&config)?)
        .with_context(|| format!("writing {}", config_path.display()))?;
    let protected: Vec<String> = session.protected.into_iter().collect();
    launch(&Launch {
        desktop_bin,
        core_bin,
        config_path,
        directory,
        host_id: session.host_id,
        protected,
        root: manager.root.clone(),
        extra: Vec::new(),
    })
}
