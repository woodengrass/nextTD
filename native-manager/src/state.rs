//! One isolated plugin state file with atomic replacement.
//!
//! Reads never fabricate state: a missing file means empty, a corrupt file is
//! an error. Writes go to a temporary sibling and are renamed over the target,
//! so a crash cannot leave a truncated `plugins.json` behind.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use fs2::FileExt as _;
use serde::{Deserialize, Serialize};

/// Immutable snapshot: enabled plugins must also be installed.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct State {
    #[serde(default)]
    pub installed: BTreeSet<String>,
    #[serde(default)]
    pub enabled: BTreeSet<String>,
}

impl State {
    /// Checked constructor; enabling something not installed is rejected.
    pub fn new(installed: BTreeSet<String>, enabled: BTreeSet<String>) -> Result<Self> {
        if !enabled.is_subset(&installed) {
            bail!("enabled plugins must be installed");
        }
        Ok(Self { installed, enabled })
    }
}

/// File-backed store for [`State`].
#[derive(Debug)]
pub struct StateStore {
    pub path: PathBuf,
}

/// Exclusive advisory lock on the `<state>.lock` sidecar, released when the
/// guard drops (the kernel releases `flock` on close, so a crashed holder
/// never leaves a stale lock).
///
/// Mutations hold this across consent, dependency installs, and the atomic
/// write, so two simultaneous operations cannot interleave. A second
/// operation fails fast with an actionable busy error instead of waiting or
/// overwriting. Reads never take it: atomic replacement keeps them consistent.
pub struct StateLock {
    _file: std::fs::File,
    target: StateStore,
}

impl StateStore {
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    /// Read the snapshot; missing file means empty, corrupt file is an error.
    pub fn read(&self) -> Result<State> {
        let text = match std::fs::read_to_string(&self.path) {
            Err(error)
                if error.kind() == std::io::ErrorKind::NotFound && !self.path.is_symlink() =>
            {
                return Ok(State::default());
            }
            Err(error) => {
                bail!("Cannot read plugin state {}: {error}", self.path.display());
            }
            Ok(text) => text,
        };
        serde_json::from_str::<State>(&text)
            .map_err(|error| {
                anyhow::anyhow!("Cannot read plugin state {}: {error}", self.path.display())
            })
            .and_then(|state| {
                State::new(state.installed, state.enabled)
                    .with_context(|| format!("Cannot read plugin state {}", self.path.display()))
            })
    }

    /// Take the mutation lock without blocking. Contention is an error naming
    /// the state file and telling the caller to retry after the in-flight
    /// operation (possibly one waiting on user consent) finishes.
    pub fn lock(&self) -> Result<StateLock> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("Cannot lock plugin state {}", self.path.display()))?;
        }
        let target = resolve_target(&self.path)?;
        check_writable(&target)
            .with_context(|| format!("Cannot lock plugin state {}", target.display()))?;
        let mut sidecar = target.as_os_str().to_owned();
        sidecar.push(".lock");
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&sidecar)
            .with_context(|| format!("Cannot lock plugin state {}", self.path.display()))?;
        // A child forked by another thread holds a copy of this descriptor
        // until it execs, which can take milliseconds under load and makes
        // the lock look busy. Retry for a bounded time, not a spin count,
        // before treating it as a concurrent holder; a genuine holder keeps
        // the lock far longer, so two mutations still fail fast below.
        let deadline = std::time::Instant::now() + std::time::Duration::from_millis(500);
        loop {
            match file.try_lock_exclusive() {
                Ok(()) => {
                    return Ok(StateLock {
                        _file: file,
                        target: StateStore::new(target),
                    });
                }
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        && std::time::Instant::now() < deadline =>
                {
                    std::thread::sleep(std::time::Duration::from_millis(1));
                }
                Err(error) => {
                    if error.kind() == std::io::ErrorKind::WouldBlock {
                        anyhow::bail!(
                            "Plugin state {} is locked by another operation; wait for it to finish and try again",
                            self.path.display()
                        );
                    }
                    return Err(anyhow::anyhow!(
                        "Cannot lock plugin state {}: {error}",
                        self.path.display()
                    ));
                }
            }
        }
    }

    /// Atomically replace the snapshot; the target is never truncated in place.
    ///
    /// The caller must hold [`StateStore::lock`]: the write itself takes no
    /// lock, so taking one here would deadlock a holder that calls back in.
    #[cfg(test)]
    pub fn write(&self, state: &State) -> Result<()> {
        let checked = State::new(state.installed.clone(), state.enabled.clone())?;
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("Cannot save plugin state {}", self.path.display()))?;
        }
        write_atomically(&self.path, serde_json::to_string_pretty(&checked)?)
            .with_context(|| format!("Cannot save plugin state {}", self.path.display()))
    }
}

impl StateLock {
    pub fn path(&self) -> &Path {
        &self.target.path
    }

    pub(crate) fn read_state(&self) -> Result<State> {
        self.target.read()
    }

    /// Rename is the commit point. Keep its subsequent durability check separate.
    pub fn replace(&self, value: &impl Serialize) -> Result<RenamedFile> {
        replace_at(self.path(), serde_json::to_string_pretty(value)?)
            .with_context(|| format!("Cannot save {}", self.path().display()))
    }
}

#[must_use = "sync the directory after committing the resources referenced by this file"]
pub struct RenamedFile {
    directory: std::fs::File,
    path: PathBuf,
}

impl RenamedFile {
    pub fn sync(self) -> Result<()> {
        self.directory.sync_all().with_context(|| {
            format!(
                "Saved {} but could not sync its directory",
                self.path.display()
            )
        })
    }
}

fn resolve_target(path: &Path) -> std::io::Result<PathBuf> {
    match path.canonicalize() {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && !path.is_symlink() => {
            Ok(parent_dir(path)
                .canonicalize()?
                .join(path.file_name().ok_or(error)?))
        }
        result => result,
    }
}

fn parent_dir(path: &Path) -> &Path {
    path.parent()
        .filter(|dir| !dir.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

fn check_writable(target: &Path) -> std::io::Result<()> {
    use rustix::fs::{Access, AtFlags, CWD, accessat};
    if target.try_exists()? {
        accessat(CWD, target, Access::WRITE_OK, AtFlags::EACCESS)?;
    }
    accessat(
        CWD,
        parent_dir(target),
        Access::WRITE_OK | Access::EXEC_OK,
        AtFlags::EACCESS,
    )?;
    Ok(())
}

#[cfg(test)]
fn write_atomically(path: &Path, text: String) -> Result<()> {
    replace_at(&resolve_target(path)?, text)?.sync()
}

fn replace_at(path: &Path, text: String) -> std::io::Result<RenamedFile> {
    check_writable(path)?;
    let parent = parent_dir(path);
    let directory = std::fs::File::open(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    use std::io::Write as _;
    temporary.write_all(text.as_bytes())?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary.persist(path)?;
    Ok(RenamedFile {
        directory,
        path: path.to_owned(),
    })
}

#[cfg(test)]
mod tests {
    use super::{State, StateStore, write_atomically};
    use anyhow::{Context, Result};
    use std::collections::BTreeSet;

    fn set(names: &[&str]) -> BTreeSet<String> {
        names.iter().map(ToString::to_string).collect()
    }

    #[test]
    fn linked_read_only_target_is_left_untouched() -> Result<()> {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let root = tempfile::tempdir()?;
        let target = root.path().join("saved");
        std::fs::write(&target, b"old")?;
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o444))?;
        let link = root.path().join("appearance.json");
        symlink(&target, &link)?;
        assert!(write_atomically(&link, "new".into()).is_err());
        assert!(StateStore::new(link.clone()).lock().is_err());
        assert!(!root.path().join("saved.lock").exists());
        assert_eq!(std::fs::read(&target)?, b"old");
        assert!(link.is_symlink());
        Ok(())
    }

    #[test]
    fn repointing_a_link_does_not_redirect_the_locked_operation() -> Result<()> {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir()?;
        let first = root.path().join("first");
        let second = root.path().join("second");
        let link = root.path().join("plugins.json");
        let state = State::new(set(&["first"]), set(&[]))?;
        std::fs::write(&first, serde_json::to_vec(&state)?)?;
        std::fs::write(&second, b"{}")?;
        symlink(&first, &link)?;
        let guard = StateStore::new(link.clone()).lock()?;
        std::fs::remove_file(&link)?;
        symlink(&second, &link)?;
        assert_eq!(guard.read_state()?, state);
        let changed = State::new(set(&["first"]), set(&["first"]))?;
        guard.replace(&changed)?.sync()?;
        assert_eq!(StateStore::new(first).read()?, changed);
        assert_eq!(std::fs::read(&second)?, b"{}");
        assert_eq!(std::fs::read_link(link)?, second);
        Ok(())
    }

    #[test]
    fn aliases_of_one_state_share_its_mutation_lock() -> Result<()> {
        let root = tempfile::tempdir()?;
        let target = root.path().join("shared");
        std::fs::write(&target, b"{}")?;
        let link = root.path().join("editor.json");
        std::os::unix::fs::symlink(&target, &link)?;
        let _guard = StateStore::new(target).lock()?;
        assert!(StateStore::new(link).lock().is_err());
        Ok(())
    }

    #[test]
    fn missing_file_reads_as_empty() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let state = StateStore::new(dir.path().join("plugins.json")).read()?;
        assert_eq!(state, State::default());
        Ok(())
    }

    #[test]
    fn corrupt_file_is_an_error_not_a_reset() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("plugins.json");
        std::fs::write(&path, "{not json")?;
        match StateStore::new(path).read() {
            Err(error) => assert!(
                error.to_string().contains("Cannot read plugin state"),
                "{error:?}"
            ),
            Ok(state) => panic!("corrupt state silently reset: {state:?}"),
        }
        Ok(())
    }

    #[test]
    fn enabled_must_be_installed() -> Result<()> {
        assert!(State::new(set(&["a"]), set(&["a", "b"])).is_err());
        assert!(State::new(set(&["a", "b"]), set(&["a"])).is_ok());
        Ok(())
    }

    #[test]
    fn roundtrip_preserves_sets() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let store = StateStore::new(dir.path().join("nested").join("plugins.json"));
        let state = State::new(set(&["a", "b"]), set(&["a"]))?;
        store.write(&state)?;
        assert_eq!(store.read()?, state);
        Ok(())
    }

    #[test]
    fn write_replaces_atomically_without_litter() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("plugins.json");
        std::fs::write(&path, "{\"installed\":[\"a\"],\"enabled\":[]}")?;
        write_atomically(&path, "{\"installed\":[],\"enabled\":[]}".to_string())
            .map_err(|error| anyhow::anyhow!("{error}"))?;
        let state: State = serde_json::from_str(
            &std::fs::read_to_string(&path).with_context(|| "target must remain readable")?,
        )?;
        assert_eq!(state, State::default());
        let mut entries = std::fs::read_dir(dir.path())?;
        let first = entries
            .next()
            .with_context(|| "expected the state file")??;
        assert!(entries.next().is_none(), "temporary sibling leaked");
        assert_eq!(first.path(), path);
        Ok(())
    }
}
