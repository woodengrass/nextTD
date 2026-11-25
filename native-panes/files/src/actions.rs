//! Native file actions for the Files pane.
//!
//! Rust port of `src/tdnext/plugins/files/filesystem.py` (atomic
//! no-overwrite operations), `editor_preferences.py` (persistent editor
//! choice) and the editor-resolution half of `view.py`
//! (`resolve_editor_command`, `edit`, restored environment).
//!
//! Public API:
//!
//! - [`create`] / [`rename`] / [`remove_confirmed`] / [`paste`] mirror the Python
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
//! - Copy timestamps from the pre-read snapshot and preserve mode (except
//!   setuid/setgid) and POSIX ACLs. Other xattrs are best-effort.
//! - Stage copies in the destination directory, verify them, and publish
//!   with a single no-replace rename. EXDEV moves clean the source only
//!   after commit. Recursive unlink never descends into another mount.
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

use std::collections::HashMap;
use std::ffi::OsStr;
use std::os::fd::AsFd as _;
use std::os::unix::ffi::OsStrExt as _;
use std::os::unix::fs::MetadataExt as _;
use std::os::unix::fs::PermissionsExt as _;
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
        .map(crate::data::single_line)
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
        return Err(already_exists(&crate::data::single_line(name)));
    }
    Ok(target)
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
pub fn create(dir: &Path, name: impl AsRef<OsStr>, folder: bool) -> Result<PathBuf, String> {
    let name = name.as_ref();
    let target = child_path_os(dir, name)?;
    if folder {
        match std::fs::create_dir(&target) {
            Ok(()) => Ok(target),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                Err(already_exists(&crate::data::single_line(name)))
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
            Err(error) if error == Errno::EXIST => {
                Err(already_exists(&crate::data::single_line(name)))
            }
            Err(error) => Err(error.to_string()),
        }
    }
}

/// Rename without ever replacing an existing destination.
pub fn rename(src: &Path, name: impl AsRef<OsStr>) -> Result<PathBuf, String> {
    let parent = src
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let target = child_path_os(&parent, name.as_ref())?;
    rename_noreplace(src, &target).map_err(RenameError::into_message)?;
    Ok(target)
}

/// Remove a file, symlink, or directory tree. A symlink to a directory
/// removes the link, never the target.
#[cfg(test)]
pub fn remove(src: &Path) -> Result<(), String> {
    remove_confirmed(src, confirm(src)?)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Identity(u64, u64, u32);
impl From<&std::fs::Metadata> for Identity {
    fn from(m: &std::fs::Metadata) -> Self {
        Self(m.dev(), m.ino(), m.mode() & libc::S_IFMT)
    }
}
pub fn identity(path: &Path) -> Result<Identity, String> {
    std::fs::symlink_metadata(path)
        .map(|m| Identity::from(&m))
        .map_err(|e| e.to_string())
}
fn pin(path: &Path) -> Result<std::fs::File, String> {
    rustix::fs::open(
        path,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map(Into::into)
    .map_err(|e| e.to_string())
}
fn fd_metadata(fd: &std::fs::File) -> Result<std::fs::Metadata, String> {
    fd.metadata().map_err(|e| e.to_string())
}
fn dir_path(dir: &std::fs::File) -> PathBuf {
    use std::os::fd::AsRawFd;
    PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd()))
}
fn parent_dir(path: &Path) -> Result<std::fs::File, String> {
    std::fs::File::open(
        path.parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new(".")),
    )
    .map_err(|e| e.to_string())
}
#[cfg(test)]
thread_local! { pub(crate) static BIND_ROOT: std::cell::Cell<Option<(u64,u64)>> = const { std::cell::Cell::new(None) }; }
pub(crate) fn mount(dir: &std::fs::File) -> Result<(u64, u64), String> {
    use rustix::fs::{AtFlags, StatxFlags, statx};
    let st = statx(dir, "", AtFlags::EMPTY_PATH, StatxFlags::MNT_ID).map_err(|e| e.to_string())?;
    if st.stx_mask & StatxFlags::MNT_ID.bits() == 0 {
        return Err("Cannot verify mount ID".into());
    }
    let m = dir.metadata().map_err(|e| e.to_string())?;
    #[cfg(test)]
    if BIND_ROOT.get() == Some((m.dev(), m.ino())) {
        return Ok((m.dev(), u64::MAX));
    }
    Ok((m.dev(), st.stx_mnt_id))
}
fn check_mount(parent: &std::fs::File, child: &std::fs::File, path: &Path) -> Result<(), String> {
    if mount(parent)? != mount(child)? {
        Err(format!(
            "Cannot remove mount point: {}",
            crate::data::single_line(path)
        ))
    } else {
        Ok(())
    }
}
pub(crate) fn checked_dir(
    parent: &std::fs::File,
    path: &Path,
    expected: Identity,
) -> Result<std::fs::File, String> {
    let name = path.file_name().ok_or("Cannot delete filesystem root")?;
    let dir: std::fs::File = open_dir_nofollow(&dir_path(parent).join(name))?.into();
    check_mount(parent, &dir, path)?;
    if Identity::from(&dir.metadata().map_err(|e| e.to_string())?) != expected {
        return Err(format!(
            "Source changed: {}",
            crate::data::single_line(path)
        ));
    }
    Ok(dir)
}
fn scan_removal(parent: &std::fs::File, path: &Path) -> Result<CopiedEntry, String> {
    let name = path.file_name().ok_or("Cannot delete filesystem root")?;
    let metadata =
        std::fs::symlink_metadata(dir_path(parent).join(name)).map_err(|e| e.to_string())?;
    let mut children = Vec::new();
    if metadata.is_dir() {
        let dir = checked_dir(parent, path, Identity::from(&metadata))?;
        for child in std::fs::read_dir(dir_path(&dir)).map_err(|e| e.to_string())? {
            children.push(scan_removal(
                &dir,
                &path.join(child.map_err(|e| e.to_string())?.file_name()),
            )?);
        }
    } else {
        check_mount(parent, &pin(&dir_path(parent).join(name))?, path)?;
    }
    Ok(CopiedEntry {
        path: path.to_owned(),
        metadata,
        children,
    })
}
pub struct Confirmed {
    // Pin the inode for the lifetime of the dialog, including on filesystems
    // that do not have birth times or have coarse timestamp resolution.
    held: std::fs::File,
    metadata: std::fs::Metadata,
}
pub fn confirm(path: &Path) -> Result<Confirmed, String> {
    let held = pin(path)?;
    Ok(Confirmed {
        metadata: fd_metadata(&held)?,
        held,
    })
}
pub fn remove_confirmed(src: &Path, confirmed: Confirmed) -> Result<(), String> {
    let parent = parent_dir(src)?;
    if !same_entry(
        &std::fs::symlink_metadata(src).map_err(|e| e.to_string())?,
        &confirmed.metadata,
    ) {
        return Err(changed(src));
    }
    let tree = scan_removal(&parent, src)?;
    if !same_entry(&tree.metadata, &confirmed.metadata)
        || !same_entry(&fd_metadata(&confirmed.held)?, &confirmed.metadata)
    {
        return Err(changed(src));
    }
    unlink_tree(&parent, &tree, &mut HashMap::new(), None)
}
fn changed(path: &Path) -> String {
    format!("Source changed: {}", crate::data::single_line(path))
}

/// Atomically recreate a symlink; fails if the destination exists.
fn copy_symlink_noreplace(
    source: &Path,
    target: &Path,
    parent: &std::fs::File,
) -> Result<Identity, String> {
    let link = std::fs::read_link(source).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            source_gone(&display_name(source))
        } else {
            error.to_string()
        }
    })?;
    let name = target.file_name().ok_or(NAME_ERROR)?;
    rustix::fs::symlinkat(&link, parent, name).map_err(|e| {
        if e == Errno::EXIST {
            already_exists(&display_name(target))
        } else {
            e.to_string()
        }
    })?;
    identity_at(parent, name)
}
fn identity_at(parent: &std::fs::File, name: &OsStr) -> Result<Identity, String> {
    let st = rustix::fs::statat(parent, name, rustix::fs::AtFlags::SYMLINK_NOFOLLOW)
        .map_err(|e| e.to_string())?;
    Ok(Identity(st.st_dev, st.st_ino, st.st_mode & libc::S_IFMT))
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
        if name.is_empty()
            || name == b"system.posix_acl_access"
            || name == b"system.posix_acl_default"
        {
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

fn timestamps(meta: &std::fs::Metadata) -> Timestamps {
    Timestamps {
        last_access: Timespec {
            tv_sec: meta.atime() as _,
            tv_nsec: meta.atime_nsec() as _,
        },
        last_modification: Timespec {
            tv_sec: meta.mtime() as _,
            tv_nsec: meta.mtime_nsec() as _,
        },
    }
}

#[cfg(test)]
thread_local! {pub(crate) static TEST_SOURCE_GROUP:std::cell::Cell<Option<u32>>=const {std::cell::Cell::new(None)};}
fn copy_access(
    meta: &std::fs::Metadata,
    source: impl rustix::fd::AsFd,
    target: impl rustix::fd::AsFd,
) -> Result<Mode, String> {
    let group = meta.gid();
    #[cfg(test)]
    let group = TEST_SOURCE_GROUP.get().unwrap_or(group);
    let keep_group = rustix::fs::fstat(&target)
        .map_err(|e| e.to_string())?
        .st_gid
        == group
        || rustix::fs::fchown(&target, None, Some(rustix::fs::Gid::from_raw(group))).is_ok();
    let mut common = (meta.mode() >> 3) & meta.mode() & 7;
    copy_xattrs_fd(&source, &target);
    for attr in ["system.posix_acl_access", "system.posix_acl_default"] {
        if attr.ends_with("default") && !meta.is_dir() {
            continue;
        }
        match rustix::fs::fgetxattr_query(&source, attr) {
            Ok(len) => {
                let mut acl = vec![0; len];
                let n = rustix::fs::fgetxattr(&source, attr, &mut acl[..])
                    .map_err(|e| e.to_string())?;
                if !keep_group {
                    // Linux POSIX ACL xattr: version followed by (tag,perm,id) records.
                    let mut shared = 7;
                    for entry in acl[4..n].chunks_exact(8) {
                        if matches!(u16::from_le_bytes([entry[0], entry[1]]), 4 | 16 | 32) {
                            shared &= entry[2];
                        }
                    }
                    if attr == "system.posix_acl_access" {
                        common &= u32::from(shared);
                    }
                    for entry in acl[4..n].chunks_exact_mut(8) {
                        if matches!(u16::from_le_bytes([entry[0], entry[1]]), 4 | 8 | 16 | 32) {
                            entry[2] &= shared;
                        }
                    }
                }
                rustix::fs::fsetxattr(&target, attr, &acl[..n], rustix::fs::XattrFlags::empty())
                    .map_err(|e| e.to_string())?;
            }
            Err(Errno::NOTSUP | Errno::NODATA) => match rustix::fs::fremovexattr(&target, attr) {
                Ok(()) | Err(Errno::NOTSUP | Errno::NODATA) => (),
                Err(e) => return Err(e.to_string()),
            },
            Err(e) => return Err(e.to_string()),
        }
    }
    Ok(Mode::from_bits_truncate(if keep_group {
        meta.mode() & 0o1777
    } else {
        (meta.mode() & 0o1700) | (common << 3) | common
    }))
}
fn finish_metadata(
    target: impl rustix::fd::AsFd,
    mode: Mode,
    meta: &std::fs::Metadata,
) -> Result<(), String> {
    rustix::fs::fchmod(&target, mode).map_err(|e| e.to_string())?;
    rustix::fs::futimens(&target, &timestamps(meta)).map_err(|e| e.to_string())?;
    rustix::fs::fsync(target).map_err(|e| e.to_string())
}

#[derive(Debug)]
pub(crate) struct CopiedEntry {
    path: PathBuf,
    metadata: std::fs::Metadata,
    children: Vec<CopiedEntry>,
}

fn same_entry(a: &std::fs::Metadata, b: &std::fs::Metadata) -> bool {
    a.created().ok() == b.created().ok()
        && a.dev() == b.dev()
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
        Err(format!(
            "Source changed: {}",
            crate::data::single_line(path)
        ))
    }
}

#[cfg(test)]
pub(crate) fn cleanup_copied(entry: &CopiedEntry) -> Result<(), String> {
    cleanup_if(entry, |_| Ok(()))
}
fn cleanup_if(
    entry: &CopiedEntry,
    before_unlink: impl Fn(&CopiedEntry) -> Result<(), String>,
) -> Result<(), String> {
    let parent = parent_dir(&entry.path)?;
    scan_removal(&parent, &entry.path)?;
    unlink_tree(&parent, entry, &mut HashMap::new(), Some(&before_unlink))
}
type CheckPublished<'a> = dyn Fn(&CopiedEntry) -> Result<(), String> + 'a;
fn unlink_tree(
    parent: &std::fs::File,
    entry: &CopiedEntry,
    links: &mut HashMap<(u64, u64), std::fs::Metadata>,
    check: Option<&CheckPublished<'_>>,
) -> Result<(), String> {
    if let Some(check) = check {
        check(entry)?;
    }
    let path = dir_path(parent).join(entry.path.file_name().ok_or(NAME_ERROR)?);
    let key = (entry.metadata.dev(), entry.metadata.ino());
    let expected = links.get(&key).unwrap_or(&entry.metadata).clone();
    let held = pin(&path)?;
    if !same_entry(&fd_metadata(&held)?, &expected) {
        return Err(changed(&entry.path));
    }
    check_mount(parent, &held, &entry.path)?;
    #[cfg(test)]
    if let Some(hook) = BEFORE_CLAIM.take() {
        hook(&path);
    }
    if expected.is_dir() {
        let dir = checked_dir(parent, &path, Identity::from(&expected))?;
        // Only the pinned source of a committed move may need
        // owner write permission to unlink children. Restore it even when
        // a conflict leaves some of the tree in place.
        let mode = Mode::from_bits_truncate(expected.mode());
        let needs_write = check.is_some()
            && !entry.children.is_empty()
            && !mode.contains(Mode::WUSR)
            && rustix::fs::accessat(&dir, ".", Access::WRITE_OK, rustix::fs::AtFlags::EACCESS)
                == Err(Errno::ACCESS);
        if needs_write {
            rustix::fs::fchmod(&dir, mode | Mode::WUSR).map_err(|e| e.to_string())?;
        }
        let mut children = Ok(());
        for child in &entry.children {
            if let Err(e) = unlink_tree(&dir, child, links, check) {
                if children.is_ok() {
                    children = Err(e);
                }
                if check.is_none() {
                    break;
                }
            }
        }
        if needs_write && let Err(e) = rustix::fs::fchmod(&dir, mode) {
            return Err(format!(
                "Cannot restore source permissions on {}: {e}",
                crate::data::single_line(&entry.path)
            ));
        }
        children?;
        if let Some(check) = check {
            check(entry)?;
        }
        if identity(&path)? != Identity::from(&expected) {
            return Err(changed(&entry.path));
        }
        return std::fs::remove_dir(&path).map_err(|e| e.to_string());
    }
    // A non-directory can be claimed and unlinked in immediate succession.
    let claim = tempfile::Builder::new()
        .prefix(".tdnext-remove-")
        .rand_bytes(16)
        .disable_cleanup(true)
        .make_in(dir_path(parent), |candidate| {
            rustix::fs::renameat_with(CWD, &path, CWD, candidate, RenameFlags::NOREPLACE)
                .map_err(std::io::Error::from)
        })
        .map_err(|e| e.to_string())?;
    let slot = claim.path();
    let result = (|| {
        let actual = std::fs::symlink_metadata(slot).map_err(|e| e.to_string())?;
        // Rename itself updates ctime. Compare to the still-open inode after
        // the rename, and to the pre-rename snapshot for the stable fields.
        if !same_entry(&actual, &fd_metadata(&held)?)
            || actual.mode() != expected.mode()
            || actual.len() != expected.len()
            || (actual.mtime(), actual.mtime_nsec()) != (expected.mtime(), expected.mtime_nsec())
        {
            return Err(changed(&entry.path));
        }
        if let Some(check) = check {
            check(entry)?;
        }
        std::fs::remove_file(slot).map_err(|e| e.to_string())
    })();
    if let Err(e) = result {
        return match rename_noreplace(slot, &path) {
            Ok(()) => Err(e),
            Err(r) => Err(format!(
                "{e}; cannot restore {} from {}: {}",
                crate::data::single_line(&entry.path),
                crate::data::single_line(
                    entry
                        .path
                        .with_file_name(slot.file_name().ok_or(NAME_ERROR)?)
                ),
                r.into_message()
            )),
        };
    }
    #[cfg(test)]
    if let Some(stop) = AFTER_UNLINK.take() {
        stop(&path);
    }
    if expected.nlink() > 1 {
        let after = fd_metadata(&held)?;
        if after.nlink() + 1 != expected.nlink()
            || after.mode() != expected.mode()
            || after.len() != expected.len()
            || (after.mtime(), after.mtime_nsec()) != (expected.mtime(), expected.mtime_nsec())
        {
            return Err(changed(&entry.path));
        }
        links.insert(key, after);
    }
    Ok(())
}

#[cfg(test)]
pub(crate) fn create_private_dir(target: &Path) -> Result<std::fs::File, String> {
    create_private_dir_at(&parent_dir(target)?, target)
}
fn create_private_dir_at(parent: &std::fs::File, target: &Path) -> Result<std::fs::File, String> {
    let name = target.file_name().ok_or(NAME_ERROR)?;
    rustix::fs::mkdirat(parent, name, Mode::RWXU).map_err(|e| {
        if e == Errno::EXIST {
            already_exists(&display_name(target))
        } else {
            e.to_string()
        }
    })?;
    rustix::fs::openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map(Into::into)
    .map_err(|e| e.to_string())
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
                path: target.canonicalize().expect("staged creation"),
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

#[cfg(test)]
pub(crate) type PathHook = Box<dyn FnOnce(&Path)>;
#[cfg(test)]
thread_local! {
    pub(crate) static AFTER_FILE_METADATA:std::cell::Cell<Option<PathHook>>=const {std::cell::Cell::new(None)};
    pub(crate) static AFTER_UNLINK:std::cell::Cell<Option<PathHook>>=const {std::cell::Cell::new(None)};
    pub(crate) static BEFORE_RECORD: std::cell::Cell<Option<PathHook>>=const {std::cell::Cell::new(None)};
    pub(crate) static BEFORE_CLAIM: std::cell::Cell<Option<PathHook>>=const {std::cell::Cell::new(None)};
}
/// Copy a single file/symlink to a path that must not exist, atomically.
fn copy_file(
    source: &Path,
    target: &Path,
    parent: &std::fs::File,
    stage: &mut Stage,
) -> Result<CopiedEntry, String> {
    let metadata = std::fs::symlink_metadata(source).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            source_gone(&display_name(source))
        } else {
            error.to_string()
        }
    })?;
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        let name = target.file_name().ok_or(NAME_ERROR)?;
        let created = copy_symlink_noreplace(source, target, parent)?;
        #[cfg(test)]
        if let Some(hook) = BEFORE_RECORD.take() {
            hook(target);
        }
        stage.created.insert(target.to_owned(), created);
        if identity_at(parent, name)? != created {
            return Err("Staging symlink replaced".into());
        }
        rustix::fs::utimensat(
            parent,
            name,
            &timestamps(&metadata),
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        )
        .map_err(|e| e.to_string())?;
        unchanged(source, &metadata)?;
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
            return Err(format!(
                "Source changed: {}",
                crate::data::single_line(source)
            ));
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
        return Err(format!(
            "Source changed: {}",
            crate::data::single_line(source)
        ));
    }
    let dest = dir_path(parent).join(target.file_name().ok_or(NAME_ERROR)?);
    let key = (opened.dev(), opened.ino());
    if let Some(previous) = stage.links.get(&key) {
        let expected = stage.created[previous];
        let held = pin(previous)?;
        let before = fd_metadata(&held)?;
        if !stage.matches(previous, &before) {
            return Err("Staged hard link changed".into());
        }
        std::fs::hard_link(previous, &dest).map_err(|e| e.to_string())?;
        stage.update_after_namespace_change(expected, &held)?;
        stage.created.insert(target.to_owned(), expected);
        return Ok(CopiedEntry {
            path: source.to_owned(),
            metadata: opened,
            children: Vec::new(),
        });
    }
    let mut outgoing = create_private_file(&dest)?;
    #[cfg(test)]
    if let Some(hook) = BEFORE_RECORD.take() {
        hook(target);
    }
    stage
        .created
        .insert(target.to_owned(), Identity::from(&fd_metadata(&outgoing)?));
    #[cfg(test)]
    note_initial(target);
    let count = std::io::copy(&mut incoming, &mut outgoing).map_err(|e| e.to_string())?;
    if count != opened.len() {
        return Err(format!(
            "Source changed: {}",
            crate::data::single_line(source)
        ));
    }
    unchanged(source, &opened)?;
    let mode = copy_access(&opened, &incoming, &outgoing)?;
    finish_metadata(&outgoing, mode, &opened)?;
    #[cfg(test)]
    if let Some(hook) = AFTER_FILE_METADATA.take() {
        hook(target);
    }
    let stored = fd_metadata(&outgoing)?;
    stage
        .stamps
        .insert(Identity::from(&stored), FileStamp::from(&stored));
    if stage.moving && opened.nlink() > 1 {
        stage.links.insert(key, target.to_owned());
    }
    Ok(CopiedEntry {
        path: source.to_owned(),
        metadata: opened,
        children: Vec::new(),
    })
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
fn copy_tree_entry(
    source: &Path,
    target: &Path,
    parent: &std::fs::File,
    stage: &mut Stage,
    outermost: bool,
) -> Result<CopiedEntry, String> {
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
        let dir = create_private_dir_at(parent, target)?;
        #[cfg(test)]
        if let Some(hook) = BEFORE_RECORD.take() {
            hook(target);
        }
        let created = Identity::from(&fd_metadata(&dir)?);
        stage.created.insert(target.to_owned(), created);
        if identity_at(parent, target.file_name().ok_or(NAME_ERROR)?)? != created {
            return Err("Staging directory replaced".into());
        }
        #[cfg(test)]
        note_initial(target);
        copy_dir_contents(source, target, stage, outermost, dir)
    } else if file_type.is_file() || file_type.is_symlink() {
        copy_file(source, target, parent, stage)
    } else {
        Err(special(&display_name(source)))
    }
}

fn copy_dir_contents(
    source: &Path,
    target: &Path,
    stage: &mut Stage,
    outermost: bool,
    target_fd: std::fs::File,
) -> Result<CopiedEntry, String> {
    let source_fd: std::fs::File = open_dir_nofollow(source)?.into();
    if stage.moving {
        check_mount(&parent_dir(source)?, &source_fd, source)?;
    }
    let metadata = source_fd.metadata().map_err(|error| error.to_string())?;
    unchanged(source, &metadata)?;
    let mut names = Vec::new();
    for entry in std::fs::read_dir(source).map_err(|error| error.to_string())? {
        names.push(entry.map_err(|error| error.to_string())?.file_name());
    }
    names.sort();
    let mut children = Vec::new();
    for name in names {
        children.push(copy_tree_entry(
            &source.join(&name),
            &target.join(&name),
            &target_fd,
            stage,
            false,
        )?);
    }
    let copied = source_fd.metadata().map_err(|error| error.to_string())?;
    if !same_entry(&metadata, &copied) {
        return Err(format!(
            "Source changed: {}",
            crate::data::single_line(source)
        ));
    }
    unchanged(source, &copied)?;
    let mode = copy_access(&metadata, source_fd.as_fd(), &target_fd)?;
    if outermost && !mode.contains(Mode::WUSR) {
        // Linux checks write permission on the directory whose `..` changes.
        rustix::fs::fchmod(&target_fd, mode | Mode::WUSR).map_err(|e| e.to_string())?;
        rustix::fs::fsync(&target_fd).map_err(|e| e.to_string())?;
        stage.root_metadata = Some((target_fd.into(), mode));
    } else {
        finish_metadata(&target_fd, mode, &metadata)?;
    }

    Ok(CopiedEntry {
        path: source.to_path_buf(),
        metadata,
        children,
    })
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct FileStamp {
    size: u64,
    mtime: (i64, i64),
    ctime: (i64, i64),
}
impl From<&std::fs::Metadata> for FileStamp {
    fn from(m: &std::fs::Metadata) -> Self {
        Self {
            size: m.len(),
            mtime: (m.mtime(), m.mtime_nsec()),
            ctime: (m.ctime(), m.ctime_nsec()),
        }
    }
}
#[derive(Default)]
struct Stage {
    stamps: HashMap<Identity, FileStamp>,
    root_metadata: Option<(rustix::fd::OwnedFd, Mode)>,
    created: HashMap<PathBuf, Identity>,
    links: HashMap<(u64, u64), PathBuf>,
    moving: bool,
}
impl Stage {
    fn matches(&self, path: &Path, m: &std::fs::Metadata) -> bool {
        let id = Identity::from(m);
        self.created.get(path) == Some(&id)
            && (!m.is_file() || self.stamps.get(&id) == Some(&FileStamp::from(m)))
    }
    // link and rename advance ctime on the pinned inode, but change neither
    // its bytes nor its stored mtime. Rebase only that part of the snapshot.
    fn update_after_namespace_change(
        &mut self,
        id: Identity,
        held: &std::fs::File,
    ) -> Result<(), String> {
        let stamp = self
            .stamps
            .get_mut(&id)
            .ok_or("Missing staged file snapshot")?;
        let now = FileStamp::from(&fd_metadata(held)?);
        if now.size != stamp.size || now.mtime != stamp.mtime {
            return Err("Staged file changed".into());
        }
        stamp.ctime = now.ctime;
        Ok(())
    }
    // Walk only recorded entries and anchor every descent at the checked directory.
    fn discard(&mut self, parent: &std::fs::File, logical: &Path) -> Result<(), String> {
        let Some(&expected) = self.created.get(logical) else {
            return Ok(());
        };
        let path = dir_path(parent).join(logical.file_name().ok_or(NAME_ERROR)?);
        if identity_at(parent, logical.file_name().ok_or(NAME_ERROR)?)? != expected {
            return Err("staging entry replaced".into());
        }
        if expected.2 == libc::S_IFDIR {
            let dir = checked_dir(parent, logical, expected)?;
            rustix::fs::fchmod(&dir, Mode::RWXU).map_err(|e| e.to_string())?;
            for child in std::fs::read_dir(dir_path(&dir)).map_err(|e| e.to_string())? {
                self.discard(
                    &dir,
                    &logical.join(child.map_err(|e| e.to_string())?.file_name()),
                )?;
            }
            if identity_at(parent, logical.file_name().ok_or(NAME_ERROR)?)? != expected {
                return Err("staging directory replaced".into());
            }
            std::fs::remove_dir(path).map_err(|e| e.to_string())
        } else if self.stamps.contains_key(&expected) {
            let held = pin(&path)?;
            let meta = fd_metadata(&held)?;
            if !self.matches(logical, &meta) {
                return Err("staging file modified".into());
            }
            std::fs::remove_file(path).map_err(|e| e.to_string())?;
            if meta.nlink() > 1 {
                self.update_after_namespace_change(expected, &held)?;
            }
            Ok(())
        } else {
            std::fs::remove_file(path).map_err(|e| e.to_string())
        }
    }
}
fn verify(entry: &CopiedEntry, path: &Path, stage: &Stage) -> Result<(), String> {
    unchanged(&entry.path, &entry.metadata)?;
    let m = std::fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if !stage.matches(path, &m)
        || m.file_type() != entry.metadata.file_type()
        || (!m.is_dir() && m.len() != entry.metadata.len())
    {
        return Err(format!(
            "Staged copy differs: {}",
            crate::data::single_line(&entry.path)
        ));
    }
    for child in &entry.children {
        verify(
            child,
            &path.join(child.path.file_name().ok_or(NAME_ERROR)?),
            stage,
        )?;
    }
    Ok(())
}
pub(crate) fn staged_copy(
    source: &Path,
    target: &Path,
    moving: bool,
) -> Result<CopiedEntry, String> {
    let parent = parent_dir(target)?;
    let name = target.file_name().ok_or(NAME_ERROR)?;
    let root = tempfile::Builder::new()
        .prefix(".tdnext-files-")
        .permissions(std::fs::Permissions::from_mode(0o700))
        .tempdir_in(dir_path(&parent))
        .map_err(|e| e.to_string())?
        .keep();
    let visible = target.with_file_name(root.file_name().ok_or(NAME_ERROR)?);
    let left = |e: String| {
        format!(
            "Staging remains at {}: {e}",
            crate::data::single_line(&visible)
        )
    };
    let dir: std::fs::File = open_dir_nofollow(&root).map_err(left)?.into();
    let id = Identity::from(&dir.metadata().map_err(|e| left(e.to_string()))?);
    let staged = dir_path(&dir).join(name);
    let mut stage = Stage {
        moving,
        ..Stage::default()
    };
    let mut committed = false;
    let result: Result<_, String> = (|| {
        let copied = copy_tree_entry(source, &staged, &dir, &mut stage, true)?;
        dir.sync_all().map_err(|e| e.to_string())?;
        #[cfg(test)]
        fire_between_note(target);
        verify(&copied, &staged, &stage)?;
        let (published, final_mode) = match stage.root_metadata.take() {
            Some((dir, mode)) => (std::fs::File::from(dir), Some(mode)),
            None => (pin(&staged)?, None),
        };
        let pinned = fd_metadata(&published)?;
        if !stage.matches(&staged, &pinned) {
            return Err("Staged object changed".into());
        }
        rename_noreplace(&staged, &dir_path(&parent).join(name))
            .map_err(RenameError::into_message)?;
        committed = true;
        if pinned.is_file() {
            stage.update_after_namespace_change(Identity::from(&pinned), &published)?;
        }
        if let Some(mode) = final_mode {
            finish_metadata(&published, mode, &copied.metadata).map_err(|e| {
                format!("Destination committed; cannot finalize directory metadata: {e}")
            })?;
        }
        Ok((copied, published))
    })();
    let cleanup = (|| {
        if !committed {
            stage.discard(&dir, &staged)?;
        }
        if identity(&root)? != id {
            return Err("staging directory replaced".into());
        }
        std::fs::remove_dir(&root).map_err(|e| e.to_string())
    })();
    if let Err(e) = cleanup {
        return Err(match result {
            Err(cause) => format!("{cause}; {}", left(e)),
            Ok(_) => format!("Destination committed; {}", left(e)),
        });
    }
    let (copy, published) = result?;
    parent
        .sync_all()
        .map_err(|e| format!("Destination committed; directory sync failed: {e}"))?;
    if moving {
        #[cfg(test)]
        fire_between_note(source);
        cleanup_if(&copy, |entry| {
            let relative = entry.path.strip_prefix(source).map_err(|e| e.to_string())?;
            let (visible, record) = if relative.as_os_str().is_empty() {
                (target.to_owned(), staged.clone())
            } else {
                (target.join(relative), staged.join(relative))
            };
            if identity(target).ok() != Some(Identity::from(&fd_metadata(&published)?))
                || !std::fs::symlink_metadata(&visible).is_ok_and(|m| stage.matches(&record, &m))
            {
                return Err(format!(
                    "Destination changed: {}",
                    crate::data::single_line(&visible)
                ));
            }
            Ok(())
        })
        .map_err(|e| format!("Partial move: destination copied, source cleanup incomplete: {e}"))?;
    }
    Ok(copy)
}

/// Move without ever replacing an existing destination. Cross-device moves
/// fall back to exclusive copy + delete so the destination is still never
/// overwritten; the source is removed only after a successful copy.
pub(crate) fn move_noreplace(source: &Path, target: &Path) -> Result<(), String> {
    if is_real_dir(source) && mount(&parent_dir(source)?)? == mount(&parent_dir(target)?)? {
        ensure_tree_copyable(source)?;
    }
    match rename_noreplace(source, target) {
        Ok(()) => Ok(()),
        Err(RenameError::Exdev) => staged_copy(source, target, true).map(drop),
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
    } else {
        staged_copy(src, &target, false)?;
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
        crate::data::single_line(path)
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
