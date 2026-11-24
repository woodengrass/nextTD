//! The `list`, `install`, `enable`, `disable`, `uninstall`, and
//! `install-zellij` subcommands.

use std::collections::BTreeSet;
use std::io::{self, Write as _};

use anyhow::{Context, Result};
use tdnext_manager::{Action, Manager, installer};

/// `approve` answers the installer's consent prompt on the terminal.
pub fn confirm(text: &str) -> bool {
    let _ = writeln!(io::stderr(), "{text}. Proceed? [y/N] ");
    let _ = io::stderr().flush();
    let mut line = String::new();
    io::stdin().read_line(&mut line).is_ok()
        && matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

fn status_of(id: &str, installed: &BTreeSet<String>, enabled: &BTreeSet<String>) -> &'static str {
    if enabled.contains(id) {
        "enabled"
    } else if installed.contains(id) {
        "installed"
    } else {
        "available"
    }
}

/// One rendered `list` row; pure for testing.
#[must_use]
pub fn list_row(id: &str, status: &str, description: &str) -> String {
    format!("{id:12} {status:10} {description}")
}

pub fn list(manager: &Manager) -> Result<()> {
    let state = manager.state()?;
    let mut stdout = io::stdout().lock();
    for manifest in manager.manifests.values() {
        let status = status_of(&manifest.id, &state.installed, &state.enabled);
        let _ = writeln!(
            stdout,
            "{}",
            list_row(&manifest.id, status, &manifest.description)
        );
    }
    for id in state
        .installed
        .difference(&manager.manifests.keys().cloned().collect())
    {
        let status = status_of(id, &state.installed, &state.enabled);
        let _ = writeln!(
            stdout,
            "{}",
            list_row(id, status, "Missing plugin manifest")
        );
    }
    for error in manager.errors.values() {
        let _ = writeln!(io::stderr(), "Invalid plugin: {error}");
    }
    Ok(())
}

fn report(action: Action, id: &str, affected: &[String]) {
    match action {
        Action::Install => println!("Installed: {}", affected.join(", ")),
        Action::Enable => println!("Enabled: {}", affected.join(", ")),
        Action::Disable if affected.is_empty() => println!("Disabled: (already disabled)"),
        Action::Disable => println!("Disabled: {}", affected.join(", ")),
        Action::Uninstall if affected.is_empty() => {
            println!("Uninstalled: {id}; Disabled: (already disabled)");
        }
        Action::Uninstall => println!("Uninstalled: {id}; Disabled: {}", affected.join(", ")),
    }
}

/// Terminal lifecycle commands run unprotected, like the Python CLI: the
/// settings surface passes the session protection set instead.
pub fn apply_and_report(manager: &Manager, action: Action, id: &str) -> Result<()> {
    let affected = manager
        .apply(action, id, &BTreeSet::new(), confirm)
        .with_context(|| format!("cannot apply {}", describe(action)))?;
    report(action, id, &affected);
    Ok(())
}

fn describe(action: Action) -> &'static str {
    match action {
        Action::Install => "install",
        Action::Enable => "enable",
        Action::Disable => "disable",
        Action::Uninstall => "uninstall",
    }
}

/// Explicit opt-in install of the pinned Zellij binary; never automatic.
pub fn install_zellij(manager: &Manager) -> Result<()> {
    let bin = manager.root.join("bin");
    let installed = installer::install("zellij", &bin)?;
    println!("Installed Zellij: {}", installed.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{list_row, report};

    #[test]
    fn rows_align_status_columns() {
        assert_eq!(
            list_row("files", "enabled", "Browse and manage local files"),
            "files        enabled    Browse and manage local files"
        );
        assert_eq!(
            list_row(
                "settings",
                "available",
                "Manage installed and enabled plugins"
            ),
            "settings     available  Manage installed and enabled plugins"
        );
    }

    #[test]
    fn reports_do_not_panic_on_empty_sets() {
        use tdnext_manager::Action;
        report(Action::Disable, "files", &[]);
        report(Action::Uninstall, "files", &[]);
        report(Action::Install, "files", &["files".to_string()]);
    }
}
