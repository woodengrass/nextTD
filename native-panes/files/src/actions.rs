//! Native file actions for the Files pane.
//!
//! Rust port of `src/tdnext/plugins/files/filesystem.py` (atomic
//! no-overwrite operations), `editor_preferences.py` (persistent editor
//! choice) and the editor-resolution half of `view.py`
//! (`resolve_editor_command`, `edit`, restored environment).
//!
//! Public API:
//!
//! - [`create`] / [`rename`] / [`remove`] / [`paste`] mirror the Python
//!   functions of the same name. `paste` collapses the Python `Clipboard`
//!   (`path`, `move`) into `(src, dir, moving)`.
//! - [`editor`] mirrors `resolve_editor_command` + the launch half of `edit`:
//!   it returns a [`std::process::Command`] whose program, arguments and
//!   environment are fully resolved. The caller suspends the terminal and
//!   runs the command, then reloads the directory.
//!
//! Preserved semantics:
//!
//! - No-overwrite everywhere: file creation uses `O_CREAT | O_EXCL`,
//!   directory creation and symlink creation fail on an existing
//!   destination, and moves use `renameat2(RENAME_NOREPLACE)` so a
//!   destination created by a rival between the existence check and the
//!   rename fails with `Already exists` instead of being overwritten. The
//!   callers never fall back to a plain (replacing) rename.
//! - Symlinks are never followed while copying: a symlink is recreated with
//!   the same link target, and directory walks use `lstat` semantics.
//! - Special files (FIFOs, sockets, devices) are refused with
//!   `Cannot copy special file: <name>` instead of being opened: opening a
//!   FIFO for reading would block the UI forever. Regular sources are opened
//!   with `O_NONBLOCK | O_NOFOLLOW` and re-validated with `fstat`, so a rival
//!   swapping a regular source for a FIFO (or symlink) between the checks
//!   cannot cause a hang or an overwrite. A partially copied destination is
//!   unlinked on failure, and a partially copied tree is removed.
//! - Metadata: regular files and directories keep their permission bits and
//!   access/modification times (the `shutil.copystat(..., follow_symlinks=
//!   False)` equivalent for links is recreating the link itself), and on
//!   Linux regular-file and directory copies also carry extended attributes
//!   best-effort (`shutil._copyxattr` semantics: unsupported filesystems or
//!   attributes never fail the copy). Symlinks never gain metadata.
//! - Cross-device moves (`EXDEV`) fall back to exclusive copy + delete, so
//!   the destination is still never overwritten and the source is removed
//!   only after a successful copy. Plain [`rename`] does not fall back,
//!   matching the Python `rename` (the `OSError` surfaces as an `Err`).
//! - Scope validation: names must be a single non-empty component (no `/`,
//!   no NUL, not `.`/`..`), and a directory cannot be copied or moved into
//!   itself (compared after canonicalization, symlinks resolved).
//! - Editor: the preference is re-read from `<state>/editor.json` on every
//!   call (default `environment`), so a running pane picks up changes. The
//!   `environment` choice runs `shlex.split($EDITOR or "vi")` exactly;
//!   `nvim`/`vi` resolve via the local `<state>/bin` first, then `PATH`,
//!   and never fall back silently. The command environment is the restored
//!   environment (original `PATH`, bootstrap markers removed).
//!
//! Exact divergences from the Python implementation:
//!
//! - OS failure strings come from `std::io` (e.g. `Invalid cross-device
//!   link (os error 18)`) rather than `OSError` (`[Errno 18] Invalid
//!   cross-device link: 'src'`). All user-facing messages (`Already exists`,
//!   `Cannot copy special file`, `Source no longer exists`, scope errors,
//!   editor errors) match exactly.
//! - `ctypes` load failure (`Atomic rename is not available`) has no Rust
//!   equivalent; an unavailable `RENAME_NOREPLACE` surfaces as
//!   `Atomic rename is not supported here: ...`.
//! - A malformed `EDITOR` value yields `Cannot start editor: could not parse
//!   EDITOR value.` (the `shlex` crate reports no reason); Python forwards
//!   the `ValueError` text.
//! - `~otheruser/...` is not expanded when resolving the state directory;
//!   only a leading `~` / `~/` is.

use std::ffi::OsStr;
use std::os::fd::AsFd as _;
use std::os::unix::ffi::OsStrExt as _;
use std::os::unix::fs::DirBuilderExt as _;
use std::os::unix::fs::MetadataExt as _;
use std::path::{Path, PathBuf};
use std::process::Command;

use rustix::fs::{Access, CWD, Mode, OFlags, RenameFlags, Timespec, Timestamps};
use rustix::io::Errno;

const INTO_ITSELF: &str = "Cannot copy or move a directory into itself.";
const NAME_ERROR: &str = "Enter one non-empty name, without a slash.";
const EDITOR_EMPTY: &str = "EDITOR is empty; set it to a terminal editor.";
const REGULAR_ONLY: &str = "Select a regular file to edit.";
const EDITOR_FILE: &str = "editor.json";

const BOOTSTRAPPED_ENV: &str = "TDNEXT_PATH_BOOTSTRAPPED";
const ORIGINAL_ENV: &str = "TDNEXT_ORIGINAL_PATH";
const ORIGINAL_PRESENT_ENV: &str = "TDNEXT_ORIGINAL_PATH_PRESENT";

fn already_exists(name: &str) -> String {
    format!("Already exists: {name}")
}

fn source_gone(name: &str) -> String {
    format!("Source no longer exists: {name}")
}

fn special(name: &str) -> String {
    format!("Cannot copy special file: {name}")
}

fn display_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Validate a single path component and join it onto `dir`, refusing to
/// overwrite anything that already exists (including symlinks).
fn child_path_os(dir: &Path, name: &OsStr) -> Result<PathBuf, String> {
    let bytes = name.as_bytes();
    if bytes.is_empty()
        || bytes == b"."
        || bytes == b".."
        || bytes.contains(&b'/')
        || bytes.contains(&0)
    {
        return Err(NAME_ERROR.to_string());
    }
    let target = dir.join(name);
    if std::fs::symlink_metadata(&target).is_ok() {
        return Err(already_exists(&name.to_string_lossy()));
    }
    Ok(target)
}

fn child_path(dir: &Path, name: &str) -> Result<PathBuf, String> {
    child_path_os(dir, OsStr::new(name))
}

fn is_symlink(path: &Path) -> bool {
    std::fs::symlink_metadata(path)
        .map(|meta| meta.file_type().is_symlink())
        .unwrap_or(false)
}

fn is_real_dir(path: &Path) -> bool {
    !is_symlink(path)
        && std::fs::metadata(path)
            .map(|meta| meta.is_dir())
            .unwrap_or(false)
}

pub(crate) enum RenameError {
    Exists(String),
    Unsupported(String),
    Exdev,
    Io(String),
}

impl RenameError {
    pub(crate) fn into_message(self) -> String {
        match self {
            RenameError::Exists(message)
            | RenameError::Unsupported(message)
            | RenameError::Io(message) => message,
            RenameError::Exdev => std::io::Error::from(Errno::XDEV).to_string(),
        }
    }
}

/// Atomically rename without replacing an existing destination.
pub(crate) fn rename_noreplace(source: &Path, target: &Path) -> Result<(), RenameError> {
    match rustix::fs::renameat_with(CWD, source, CWD, target, RenameFlags::NOREPLACE) {
        Ok(()) => Ok(()),
        Err(error) => {
            if error == Errno::EXIST {
                return Err(RenameError::Exists(already_exists(&display_name(target))));
            }
            if error == Errno::NOSYS || error == Errno::INVAL {
                return Err(RenameError::Unsupported(format!(
                    "Atomic rename is not supported here: {error}"
                )));
            }
            if error == Errno::XDEV {
                return Err(RenameError::Exdev);
            }
            Err(RenameError::Io(error.to_string()))
        }
    }
}

/// Create a file or folder; fails if the target already exists.
pub fn create(dir: &Path, name: &str, folder: bool) -> Result<PathBuf, String> {
    let target = child_path(dir, name)?;
    if folder {
        match std::fs::create_dir(&target) {
            Ok(()) => Ok(target),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                Err(already_exists(name))
            }
            Err(error) => Err(error.to_string()),
        }
    } else {
        // O_EXCL fails atomically if a rival created the path (file,
        // directory, or symlink) after child_path checked.
        match rustix::fs::open(
            &target,
            OFlags::CREATE | OFlags::EXCL | OFlags::WRONLY,
            Mode::from_bits_truncate(0o666),
        ) {
            Ok(fd) => {
                drop(fd);
                Ok(target)
            }
            Err(error) if error == Errno::EXIST => Err(already_exists(name)),
            Err(error) => Err(error.to_string()),
        }
    }
}

/// Rename without ever replacing an existing destination.
pub fn rename(src: &Path, name: &str) -> Result<PathBuf, String> {
    let parent = src
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let target = child_path(&parent, name)?;
    rename_noreplace(src, &target).map_err(RenameError::into_message)?;
    Ok(target)
}

/// Remove a file, symlink, or directory tree. A symlink to a directory
/// removes the link, never the target.
pub fn remove(src: &Path) -> Result<(), String> {
    let file_type = std::fs::symlink_metadata(src)
        .map_err(|error| error.to_string())?
        .file_type();
    if file_type.is_dir() && !file_type.is_symlink() {
        std::fs::remove_dir_all(src).map_err(|error| error.to_string())
    } else {
        std::fs::remove_file(src).map_err(|error| error.to_string())
    }
}

/// Atomically recreate a symlink; fails if the destination exists.
fn copy_symlink_noreplace(source: &Path, target: &Path) -> Result<(), String> {
    let link = std::fs::read_link(source).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            source_gone(&display_name(source))
        } else {
            error.to_string()
        }
    })?;
    match std::os::unix::fs::symlink(&link, target) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            Err(already_exists(&display_name(target)))
        }
        Err(error) => Err(error.to_string()),
    }
}

/// Best-effort extended-attribute copy mirroring `shutil._copyxattr`:
/// attribute-list failures and per-attribute failures are swallowed so an
/// unsupported filesystem never fails a normal copy. Only ever called for
/// regular files and directories, never for symlinks.
fn copy_xattrs_fd(source: impl rustix::fd::AsFd, target: impl rustix::fd::AsFd) {
    let list_len = match rustix::fs::flistxattr_query(&source) {
        Ok(len) => len,
        Err(_) => return,
    };
    if list_len == 0 {
        return;
    }
    let mut list = Vec::with_capacity(list_len.saturating_add(1));
    if rustix::fs::flistxattr(&source, rustix::buffer::spare_capacity(&mut list)).is_err() {
        return;
    }
    for name in list.split(|byte| *byte == 0) {
        if name.is_empty() {
            continue;
        }
        let value_len = match rustix::fs::fgetxattr_query(&source, name) {
            Ok(len) => len,
            Err(_) => continue,
        };
        let mut value = Vec::with_capacity(value_len.saturating_add(1));
        if rustix::fs::fgetxattr(&source, name, rustix::buffer::spare_capacity(&mut value)).is_err()
        {
            continue;
        }
        let _ = rustix::fs::fsetxattr(&target, name, &value, rustix::fs::XattrFlags::empty());
    }
}

fn open_dir_nofollow(path: &Path) -> Result<rustix::fd::OwnedFd, String> {
    rustix::fs::open(
        path,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| error.to_string())
}

fn copy_metadata(meta: &std::fs::Metadata, target: impl rustix::fd::AsFd) -> Result<(), String> {
    rustix::fs::fchmod(&target, Mode::from_bits_truncate(meta.mode() & 0o7777))
        .map_err(|error| error.to_string())?;
    let times = Timestamps {
        last_access: Timespec {
            tv_sec: meta.atime() as _,
            tv_nsec: meta.atime_nsec() as _,
        },
        last_modification: Timespec {
            tv_sec: meta.mtime() as _,
            tv_nsec: meta.mtime_nsec() as _,
        },
    };
    let _ = rustix::fs::futimens(target, &times);
    Ok(())
}

#[derive(Debug)]
pub(crate) struct CopiedEntry {
    path: PathBuf,
    metadata: std::fs::Metadata,
    children: Vec<CopiedEntry>,
}

fn same_entry(a: &std::fs::Metadata, b: &std::fs::Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.mode() == b.mode()
        && a.len() == b.len()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
        && a.ctime() == b.ctime()
        && a.ctime_nsec() == b.ctime_nsec()
}

fn unchanged(path: &Path, metadata: &std::fs::Metadata) -> Result<(), String> {
    let current = std::fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    if same_entry(metadata, &current) {
        Ok(())
    } else {
        Err(format!("Source changed: {}", path.display()))
    }
}

pub(crate) fn cleanup_copied(entry: &CopiedEntry) -> Result<(), String> {
    unchanged(&entry.path, &entry.metadata)?;
    if entry.metadata.is_dir() {
        for child in &entry.children {
            cleanup_copied(child)?;
        }
        let current = std::fs::symlink_metadata(&entry.path).map_err(|error| error.to_string())?;
        if current.dev() != entry.metadata.dev()
            || current.ino() != entry.metadata.ino()
            || !current.is_dir()
        {
            return Err(format!("Source changed: {}", entry.path.display()));
        }
        std::fs::remove_dir(&entry.path).map_err(|error| error.to_string())
    } else {
        std::fs::remove_file(&entry.path).map_err(|error| error.to_string())
    }
}

pub(crate) fn create_private_dir(target: &Path) -> Result<(), String> {
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(target)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::AlreadyExists {
                already_exists(&display_name(target))
            } else {
                error.to_string()
            }
        })
}

pub(crate) fn create_private_file(target: &Path) -> Result<std::fs::File, String> {
    rustix::fs::open(
        target,
        OFlags::CREATE | OFlags::EXCL | OFlags::WRONLY,
        Mode::from_bits_truncate(0o600),
    )
    .map(Into::into)
    .map_err(|error| {
        if error == Errno::EXIST {
            already_exists(&display_name(target))
        } else {
            error.to_string()
        }
    })
}

/// Test-only observation of destination creation, compiled out of production.
/// [`note_initial`] is called by the copy callers right after a destination
/// is created and before any bytes, children, xattrs, or final metadata are
/// written, so the record holds even if the private helpers above are
/// bypassed. Records accumulate in one log; tests take only the entries
/// under their own fixture path, so parallel tests never interfere.
#[cfg(test)]
#[derive(Debug)]
pub(crate) struct InitialMode {
    pub path: PathBuf,
    pub mode: u32,
    pub len: u64,
}

#[cfg(test)]
static INITIAL_MODES: std::sync::Mutex<Vec<InitialMode>> = std::sync::Mutex::new(Vec::new());

#[cfg(test)]
fn note_initial(target: &Path) {
    if let Ok(meta) = std::fs::symlink_metadata(target) {
        INITIAL_MODES
            .lock()
            .expect("initial-mode lock")
            .push(InitialMode {
                path: target.to_path_buf(),
                mode: meta.mode() & 0o777,
                len: meta.len(),
            });
    }
}

#[cfg(test)]
pub(crate) fn take_initial_modes_under(root: &Path) -> Vec<InitialMode> {
    INITIAL_MODES
        .lock()
        .expect("initial-mode lock")
        .extract_if(.., |entry| entry.path.starts_with(root))
        .collect()
}

#[cfg(test)]
type BetweenNote = Vec<(PathBuf, Box<dyn FnOnce() + Send>)>;

#[cfg(test)]
static BETWEEN_NOTE: std::sync::Mutex<BetweenNote> = std::sync::Mutex::new(Vec::new());

#[cfg(test)]
pub(crate) fn stash_between_note(source: &Path, note: impl FnOnce() + Send + 'static) {
    BETWEEN_NOTE
        .lock()
        .expect("between-note lock")
        .push((source.to_path_buf(), Box::new(note)));
}

#[cfg(test)]
fn fire_between_note(source: &Path) {
    let note = {
        let mut notes = BETWEEN_NOTE.lock().expect("between-note lock");
        notes
            .iter()
            .position(|(path, _)| path == source)
            .map(|index| notes.swap_remove(index).1)
    };
    if let Some(note) = note {
        note();
    }
}

/// Copy a single file/symlink to a path that must not exist, atomically.
pub(crate) fn copy_file_noreplace(source: &Path, target: &Path) -> Result<CopiedEntry, String> {
    let metadata = std::fs::symlink_metadata(source).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            source_gone(&display_name(source))
        } else {
            error.to_string()
        }
    })?;
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        copy_symlink_noreplace(source, target)?;
        if let Err(error) = unchanged(source, &metadata) {
            let _ = std::fs::remove_file(target);
            return Err(error);
        }
        return Ok(CopiedEntry {
            path: source.to_path_buf(),
            metadata,
            children: Vec::new(),
        });
    }
    if !file_type.is_file() {
        return Err(special(&display_name(source)));
    }
    let source_fd = match rustix::fs::open(
        source,
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW,
        Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(error) if error == Errno::LOOP => {
            return Err(format!("Source changed: {}", source.display()));
        }
        Err(error) if error == Errno::NOENT => {
            return Err(source_gone(&display_name(source)));
        }
        Err(error) if error == Errno::NXIO => {
            return Err(special(&display_name(source)));
        }
        Err(error) => return Err(error.to_string()),
    };
    let mut incoming: std::fs::File = source_fd.into();
    let opened = incoming.metadata().map_err(|error| error.to_string())?;
    if !opened.is_file() {
        return Err(special(&display_name(source)));
    }
    if !same_entry(&metadata, &opened) {
        return Err(format!("Source changed: {}", source.display()));
    }
    let mut outgoing = create_private_file(target)?;
    #[cfg(test)]
    note_initial(target);
    let result = (|| {
        std::io::copy(&mut incoming, &mut outgoing).map_err(|error| error.to_string())?;
        let copied = incoming.metadata().map_err(|error| error.to_string())?;
        if !same_entry(&opened, &copied) {
            return Err(format!("Source changed: {}", source.display()));
        }
        unchanged(source, &copied)?;
        copy_xattrs_fd(incoming.as_fd(), outgoing.as_fd());
        copy_metadata(&copied, outgoing.as_fd())?;
        Ok(CopiedEntry {
            path: source.to_path_buf(),
            metadata: copied,
            children: Vec::new(),
        })
    })();
    if result.is_err() {
        drop(outgoing);
        let _ = std::fs::remove_file(target);
    }
    result
}

/// Refuse trees containing special files before copying or moving them.
fn ensure_tree_copyable(source: &Path) -> Result<(), String> {
    let mut stack = vec![source.to_path_buf()];
    while let Some(current) = stack.pop() {
        let file_type = match std::fs::symlink_metadata(&current) {
            Ok(meta) => meta.file_type(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Err(source_gone(&display_name(source)));
            }
            Err(error) => return Err(error.to_string()),
        };
        if file_type.is_symlink() || file_type.is_file() {
            continue;
        }
        if file_type.is_dir() {
            let mut names = Vec::new();
            match std::fs::read_dir(&current) {
                Ok(entries) => {
                    for entry in entries {
                        match entry {
                            Ok(entry) => names.push(entry.file_name()),
                            Err(error) => return Err(error.to_string()),
                        }
                    }
                }
                Err(error)
                    if error.kind() == std::io::ErrorKind::NotFound
                        || error.kind() == std::io::ErrorKind::NotADirectory =>
                {
                    return Err(source_gone(&display_name(source)));
                }
                Err(error) => return Err(error.to_string()),
            }
            names.sort();
            for name in names {
                stack.push(current.join(name));
            }
            continue;
        }
        return Err(special(&display_name(&current)));
    }
    Ok(())
}

/// Copy one tree entry without following symlinks or opening FIFOs.
fn copy_tree_entry(source: &Path, target: &Path) -> Result<CopiedEntry, String> {
    let file_type = std::fs::symlink_metadata(source)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                source_gone(&display_name(source))
            } else {
                error.to_string()
            }
        })?
        .file_type();
    if file_type.is_dir() {
        create_private_dir(target)?;
        #[cfg(test)]
        note_initial(target);
        copy_dir_contents(source, target)
    } else if file_type.is_file() || file_type.is_symlink() {
        copy_file_noreplace(source, target)
    } else {
        Err(special(&display_name(source)))
    }
}

fn copy_dir_contents(source: &Path, target: &Path) -> Result<CopiedEntry, String> {
    let source_fd: std::fs::File = open_dir_nofollow(source)?.into();
    let target_fd = open_dir_nofollow(target)?;
    let metadata = source_fd.metadata().map_err(|error| error.to_string())?;
    unchanged(source, &metadata)?;
    let mut names = Vec::new();
    for entry in std::fs::read_dir(source).map_err(|error| error.to_string())? {
        names.push(entry.map_err(|error| error.to_string())?.file_name());
    }
    names.sort();
    let mut children = Vec::new();
    for name in names {
        children.push(copy_tree_entry(&source.join(&name), &target.join(&name))?);
    }
    let copied = source_fd.metadata().map_err(|error| error.to_string())?;
    if !same_entry(&metadata, &copied) {
        return Err(format!("Source changed: {}", source.display()));
    }
    unchanged(source, &copied)?;
    copy_xattrs_fd(source_fd.as_fd(), &target_fd);
    copy_metadata(&copied, &target_fd)?;
    Ok(CopiedEntry {
        path: source.to_path_buf(),
        metadata: copied,
        children,
    })
}

pub(crate) fn copy_dir_noreplace(source: &Path, target: &Path) -> Result<CopiedEntry, String> {
    ensure_tree_copyable(source)?;
    create_private_dir(target)?;
    #[cfg(test)]
    note_initial(target);
    let result = copy_dir_contents(source, target);
    if result.is_err() {
        let _ = std::fs::remove_dir_all(target);
    }
    result
}

/// Move without ever replacing an existing destination. Cross-device moves
/// fall back to exclusive copy + delete so the destination is still never
/// overwritten; the source is removed only after a successful copy.
pub(crate) fn move_noreplace(source: &Path, target: &Path) -> Result<(), String> {
    if is_real_dir(source) {
        ensure_tree_copyable(source)?;
    }
    match rename_noreplace(source, target) {
        Ok(()) => Ok(()),
        Err(RenameError::Exdev) => {
            let copied = if is_real_dir(source) {
                copy_dir_noreplace(source, target)?
            } else {
                copy_file_noreplace(source, target)?
            };
            #[cfg(test)]
            fire_between_note(source);
            cleanup_copied(&copied).map_err(|error| {
                format!("Partial move: destination copied, source cleanup incomplete: {error}")
            })
        }
        Err(error) => Err(error.into_message()),
    }
}

/// Copy or move `src` into directory `dir` under its own file name, without
/// ever replacing an existing destination.
pub fn paste(src: &Path, dir: &Path, moving: bool) -> Result<PathBuf, String> {
    let base = src.file_name().ok_or_else(|| NAME_ERROR.to_string())?;
    let target = child_path_os(dir, base)?;
    if is_real_dir(src) {
        let canonical_source = std::fs::canonicalize(src).map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                source_gone(&display_name(src))
            } else {
                error.to_string()
            }
        })?;
        let canonical_dir = std::fs::canonicalize(dir).map_err(|error| error.to_string())?;
        if canonical_dir == canonical_source || canonical_dir.starts_with(&canonical_source) {
            return Err(INTO_ITSELF.to_string());
        }
    }
    if moving {
        move_noreplace(src, &target)?;
    } else if is_real_dir(src) {
        copy_dir_noreplace(src, &target)?;
    } else {
        copy_file_noreplace(src, &target)?;
    }
    Ok(target)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EditorChoice {
    Environment,
    Nvim,
    Vi,
}

impl EditorChoice {
    fn from_name(name: &str) -> Option<Self> {
        match name {
            "environment" => Some(EditorChoice::Environment),
            "nvim" => Some(EditorChoice::Nvim),
            "vi" => Some(EditorChoice::Vi),
            _ => None,
        }
    }

    fn binary_name(self) -> &'static str {
        match self {
            EditorChoice::Environment => "",
            EditorChoice::Nvim => "nvim",
            EditorChoice::Vi => "vi",
        }
    }
}

pub(crate) fn expanduser(value: &str) -> String {
    if (value == "~" || value.starts_with("~/"))
        && let Ok(home) = std::env::var("HOME")
        && !home.is_empty()
    {
        if value == "~" {
            return home;
        }
        return format!("{home}{}", &value[1..]);
    }
    value.to_string()
}

/// Resolve the state directory from explicit sources (pure core of
/// [`resolve_state_dir`], directly testable without touching the process
/// environment): `TDNEXT_STATE_DIR`, then `$XDG_DATA_HOME/tdnext`, then
/// `~/.local/share/tdnext`.
pub(crate) fn resolve_state_dir_from(
    tdnext_state_dir: Option<&str>,
    xdg_data_home: Option<&str>,
    home: Option<&str>,
    cwd: Option<&Path>,
) -> PathBuf {
    if let Some(configured) = tdnext_state_dir
        && !configured.is_empty()
    {
        let path = PathBuf::from(expanduser(configured));
        if path.is_absolute() {
            return path;
        }
        if let Some(cwd) = cwd {
            return cwd.join(path);
        }
        return path;
    }
    if let Some(configured) = xdg_data_home
        && !configured.trim().is_empty()
    {
        let base = PathBuf::from(expanduser(configured));
        if base.is_absolute() {
            return base.join("tdnext");
        }
    }
    if let Some(home) = home
        && !home.is_empty()
    {
        return PathBuf::from(home)
            .join(".local")
            .join("share")
            .join("tdnext");
    }
    PathBuf::from("tdnext-state")
}

/// Resolve the state directory: `TDNEXT_STATE_DIR`, then
/// `$XDG_DATA_HOME/tdnext`, then `~/.local/share/tdnext`.
fn resolve_state_dir() -> PathBuf {
    let cwd = std::env::current_dir().ok();
    resolve_state_dir_from(
        std::env::var("TDNEXT_STATE_DIR").ok().as_deref(),
        std::env::var("XDG_DATA_HOME").ok().as_deref(),
        std::env::var("HOME").ok().as_deref(),
        cwd.as_deref(),
    )
}

fn state_bin() -> PathBuf {
    resolve_state_dir().join("bin")
}

/// Read the stored editor choice; a missing file means the default.
fn read_editor_choice() -> Result<EditorChoice, String> {
    let path = resolve_state_dir().join(EDITOR_FILE);
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(EditorChoice::Environment);
        }
        Err(error) => {
            return Err(preferences_error(&path, &error.to_string()));
        }
    };
    parse_editor_choice(&text, &path)
}

fn preferences_error(path: &Path, detail: &str) -> String {
    format!(
        "Cannot read editor preferences {}: {detail}",
        path.display()
    )
}

pub(crate) fn parse_editor_choice(text: &str, path: &Path) -> Result<EditorChoice, String> {
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|error| preferences_error(path, &error.to_string()))?;
    let object = value
        .as_object()
        .ok_or_else(|| preferences_error(path, "expected a JSON object"))?;
    for key in object.keys() {
        if key != "editor" {
            return Err(preferences_error(
                path,
                &format!("unexpected field '{key}'"),
            ));
        }
    }
    match object.get("editor") {
        // Absent means the default; an explicit null is rejected so a
        // half-written preferences file cannot silently reset the choice.
        None => Ok(EditorChoice::Environment),
        Some(serde_json::Value::String(name)) => EditorChoice::from_name(name)
            .ok_or_else(|| preferences_error(path, &format!("unknown editor '{name}'"))),
        Some(_) => Err(preferences_error(path, "'editor' must be a string")),
    }
}

fn is_executable(path: &Path) -> bool {
    path.is_file() && rustix::fs::access(path, Access::EXEC_OK).is_ok()
}

/// Local `local_bin` first, then `PATH` — never falls back silently.
pub(crate) fn find_executable_in(
    name: &str,
    local_bin: &Path,
    path_var: Option<&OsStr>,
) -> Option<String> {
    let local = local_bin.join(name);
    if is_executable(&local) {
        return Some(local.to_string_lossy().into_owned());
    }
    if let Some(paths) = path_var {
        for dir in std::env::split_paths(paths) {
            let candidate = dir.join(name);
            if is_executable(&candidate) {
                return Some(candidate.to_string_lossy().into_owned());
            }
        }
    }
    None
}

fn resolve_editor_argv() -> Result<Vec<String>, String> {
    resolve_editor_argv_with(
        read_editor_choice()?,
        std::env::var("EDITOR").ok(),
        &state_bin(),
        std::env::var_os("PATH").as_deref(),
    )
}

pub(crate) fn resolve_editor_argv_with(
    choice: EditorChoice,
    editor_env: Option<String>,
    local_bin: &Path,
    path_var: Option<&OsStr>,
) -> Result<Vec<String>, String> {
    match choice {
        EditorChoice::Environment => {
            let raw = editor_env.unwrap_or_else(|| "vi".to_string());
            match shlex::split(&raw) {
                Some(command) if !command.is_empty() => Ok(command),
                Some(_) => Err(EDITOR_EMPTY.to_string()),
                None => Err("Cannot start editor: could not parse EDITOR value.".to_string()),
            }
        }
        _ => {
            let name = choice.binary_name();
            find_executable_in(name, local_bin, path_var)
                .map(|binary| vec![binary])
                .ok_or_else(|| {
                    format!(
                        "Editor '{name}' not found; install it or choose the environment editor."
                    )
                })
        }
    }
}

/// What to do with `PATH` when restoring the environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum RestoredPath {
    /// Not bootstrapped: leave `PATH` (and other variables) untouched.
    Keep,
    /// Bootstrapped with no original `PATH`: remove it.
    Remove,
    /// Bootstrapped with a captured original `PATH`: restore it.
    Set(String),
}

/// Pure decision core of [`apply_restored_env`]: bootstrap markers are
/// accepted only when complete and self-consistent, exactly like
/// `tdnext.core.local_env._markers_valid`.
pub(crate) fn restored_path_plan(
    bootstrapped: Option<&str>,
    present_flag: Option<&str>,
    original: Option<&str>,
) -> RestoredPath {
    let valid = bootstrapped == Some("1")
        && (present_flag == Some("0") || present_flag == Some("1"))
        && (present_flag == Some("0") || original.is_some());
    if !valid {
        return RestoredPath::Keep;
    }
    if present_flag == Some("1") {
        match original {
            Some(value) => RestoredPath::Set(value.to_string()),
            None => RestoredPath::Keep,
        }
    } else {
        RestoredPath::Remove
    }
}

/// Copy the process environment with `PATH` restored and bootstrap metadata
/// removed, mirroring `tdnext.core.local_env.restored_env`.
fn apply_restored_env(command: &mut Command) {
    let plan = restored_path_plan(
        std::env::var(BOOTSTRAPPED_ENV).ok().as_deref(),
        std::env::var(ORIGINAL_PRESENT_ENV).ok().as_deref(),
        std::env::var(ORIGINAL_ENV).ok().as_deref(),
    );
    command.env_remove(BOOTSTRAPPED_ENV);
    command.env_remove(ORIGINAL_ENV);
    command.env_remove(ORIGINAL_PRESENT_ENV);
    match plan {
        RestoredPath::Keep => {}
        RestoredPath::Remove => {
            command.env_remove("PATH");
        }
        RestoredPath::Set(value) => {
            command.env("PATH", value);
        }
    }
}

/// Resolve the editor launch command for `file`.
///
/// Reads the stored preference on every call so a running pane picks up
/// changes. The caller suspends the terminal and runs the returned command
/// with `file` appended, then reloads the directory.
pub fn editor(file: &Path) -> Result<Command, String> {
    if !std::fs::metadata(file)
        .map(|meta| meta.is_file())
        .unwrap_or(false)
    {
        return Err(REGULAR_ONLY.to_string());
    }
    let argv = resolve_editor_argv()?;
    let file_arg = if file.is_absolute() {
        file.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(file))
            .unwrap_or_else(|_| file.to_path_buf())
    };
    let mut command = Command::new(&argv[0]);
    if argv.len() > 1 {
        command.args(&argv[1..]);
    }
    command.arg(file_arg);
    apply_restored_env(&mut command);
    Ok(command)
}
