//! Where the state directory lives.
//!
//! Precedence: `TDNEXT_STATE_DIR`, the checkout `state/` directory when the
//! running executable sits inside a real project checkout, otherwise the XDG
//! data location. Resolution never creates, migrates, or overwrites anything.

use std::path::{Path, PathBuf};

use anyhow::Result;

/// Resolve the state directory without touching the filesystem.
pub(crate) fn resolve_state_dir() -> Result<PathBuf> {
    if let Some(explicit) = std::env::var_os("TDNEXT_STATE_DIR") {
        let path = expanduser(&PathBuf::from(explicit));
        if path.is_absolute() {
            return Ok(path);
        }
        return Ok(std::env::current_dir()?.join(path));
    }
    if let Some(root) = find_checkout_root()? {
        return Ok(root.join("state"));
    }
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|base| base.is_absolute())
        .unwrap_or_else(|| home_dir().join(".local").join("share"));
    Ok(base.join("tdnext"))
}

/// Walk up from the running executable to a real project checkout: a
/// `pyproject.toml` naming this project beside its `src/tdnext` layout.
///
/// The search is anchored to the compiled executable location, never the
/// working directory: an installed binary resolves the XDG data directory no
/// matter where the user invoked it from, while merely visiting a checkout
/// does not redirect state there.
fn find_checkout_root() -> Result<Option<PathBuf>> {
    let executable = std::env::current_exe()?;
    let Some(dir) = executable.parent() else {
        return Ok(None);
    };
    find_checkout_root_from(dir)
}

/// Nearest ancestor of `start` holding the project marker, if any.
fn find_checkout_root_from(start: &Path) -> Result<Option<PathBuf>> {
    let mut current = start.to_path_buf();
    loop {
        let marker = current.join("pyproject.toml");
        if marker.is_file() && current.join("src").join("tdnext").is_dir() {
            let text = std::fs::read_to_string(&marker)?;
            if text.contains("terminal-desktop-next") {
                return Ok(Some(current));
            }
        }
        if !current.pop() {
            return Ok(None);
        }
    }
}

/// Expand a leading `~` against `HOME`; anything else passes through.
fn expanduser(path: &Path) -> PathBuf {
    let mut components = path.components();
    if matches!(components.next(), Some(std::path::Component::Normal(first)) if first == "~") {
        return home_dir().join(components.as_path());
    }
    path.to_path_buf()
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME").map_or_else(|| PathBuf::from("/root"), PathBuf::from)
}

#[cfg(test)]
mod tests {
    use super::find_checkout_root_from;
    use anyhow::Result;

    #[test]
    fn checkout_detection_starts_from_the_executable() -> Result<()> {
        let checkout = tempfile::tempdir()?;
        std::fs::write(
            checkout.path().join("pyproject.toml"),
            "[project]\nname = \"terminal-desktop-next\"\n",
        )?;
        std::fs::create_dir_all(checkout.path().join("src").join("tdnext"))?;
        let deep = checkout
            .path()
            .join("native-manager")
            .join("target")
            .join("debug");
        std::fs::create_dir_all(&deep)?;
        assert_eq!(
            find_checkout_root_from(&deep)?,
            Some(checkout.path().to_path_buf())
        );
        let bare = tempfile::tempdir()?;
        assert_eq!(find_checkout_root_from(bare.path())?, None);
        let wrong = tempfile::tempdir()?;
        std::fs::write(
            wrong.path().join("pyproject.toml"),
            "[project]\nname = \"something-else\"\n",
        )?;
        std::fs::create_dir_all(wrong.path().join("src").join("tdnext"))?;
        assert_eq!(find_checkout_root_from(wrong.path())?, None);
        Ok(())
    }
}
