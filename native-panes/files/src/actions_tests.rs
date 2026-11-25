//! Tests for [`crate::actions`].
//!
//! Wiring (owned by the crate root, not this file): the pane crate declares
//! `mod actions;` plus `#[cfg(test)] mod actions_tests;` so this module can
//! reach the implementation as `crate::actions`, including its `pub(crate)`
//! seams. Filesystem behavior is tested through the public API in isolated
//! temporary directories; environment-dependent decisions (editor preference,
//! state directory, restored `PATH`) are tested through the pure `pub(crate)`
//! cores, so no test mutates process-global state.

use std::ffi::{OsStr, OsString};
use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
use std::path::Path;

use crate::actions::{
    EditorChoice, RestoredPath, cleanup_copied, create, create_private_dir, create_private_file,
    editor, expanduser, find_executable_in, move_noreplace, parse_editor_choice, paste, remove,
    rename, rename_noreplace, resolve_editor_argv_with, resolve_state_dir_from, restored_path_plan,
    staged_copy,
};

fn dir() -> Result<tempfile::TempDir, String> {
    tempfile::TempDir::new().map_err(|error| error.to_string())
}

fn write(path: &Path, content: &[u8]) -> Result<(), String> {
    std::fs::write(path, content).map_err(|error| error.to_string())
}

fn make_executable(path: &Path) -> Result<(), String> {
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
        .map_err(|error| error.to_string())
}

fn make_fifo(path: &Path) -> Result<(), String> {
    rustix::fs::mkfifoat(
        rustix::fs::CWD,
        path,
        rustix::fs::Mode::from_bits_truncate(0o644),
    )
    .map_err(|error| error.to_string())
}

fn read(path: &Path) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|error| error.to_string())
}

fn mode_of(path: &Path) -> Result<u32, String> {
    Ok(path.metadata().map_err(|error| error.to_string())?.mode() & 0o777)
}

fn times_of(path: &Path) -> Result<(i64, i64, i64, i64), String> {
    let meta = path.metadata().map_err(|error| error.to_string())?;
    Ok((
        meta.atime(),
        meta.atime_nsec(),
        meta.mtime(),
        meta.mtime_nsec(),
    ))
}

fn set_times(path: &Path, atime_sec: i64, mtime_sec: i64) -> Result<(), String> {
    let times = rustix::fs::Timestamps {
        last_access: rustix::fs::Timespec {
            tv_sec: atime_sec as _,
            tv_nsec: 0,
        },
        last_modification: rustix::fs::Timespec {
            tv_sec: mtime_sec as _,
            tv_nsec: 0,
        },
    };
    rustix::fs::utimensat(rustix::fs::CWD, path, &times, rustix::fs::AtFlags::empty())
        .map_err(|error| error.to_string())
}

/// Set a user xattr; `Ok(false)` when the filesystem cannot hold xattrs so
/// the caller asserts copy success instead of xattr preservation.
fn try_set_xattr(path: &Path, name: &str, value: &[u8]) -> Result<bool, String> {
    match rustix::fs::setxattr(path, name, value, rustix::fs::XattrFlags::empty()) {
        Ok(()) => Ok(true),
        Err(_) => Ok(false),
    }
}

fn get_xattr(path: &Path, name: &str) -> Result<Vec<u8>, String> {
    let len = rustix::fs::getxattr_query(path, name).map_err(|error| error.to_string())?;
    let mut value = Vec::with_capacity(len.saturating_add(1));
    rustix::fs::getxattr(path, name, rustix::buffer::spare_capacity(&mut value))
        .map_err(|error| error.to_string())?;
    Ok(value)
}

#[test]
fn create_file_and_folder() -> Result<(), String> {
    let root = dir()?;
    let file = create(root.path(), "note.txt", false)?;
    if !file.is_file() {
        return Err("created file is missing".to_string());
    }
    let folder = create(root.path(), "docs", true)?;
    if !folder.is_dir() {
        return Err("created folder is missing".to_string());
    }
    Ok(())
}

#[test]
fn create_never_overwrites() -> Result<(), String> {
    let root = dir()?;
    write(&root.path().join("taken"), b"x")?;
    match create(root.path(), "taken", false) {
        Err(message) if message == "Already exists: taken" => {}
        other => {
            return Err(format!(
                "file over file: expected Already exists, got {other:?}"
            ));
        }
    }
    match create(root.path(), "taken", true) {
        Err(message) if message == "Already exists: taken" => {}
        other => {
            return Err(format!(
                "folder over file: expected Already exists, got {other:?}"
            ));
        }
    }
    create(root.path(), "docs", true)?;
    match create(root.path(), "docs", false) {
        Err(message) if message == "Already exists: docs" => Ok(()),
        other => Err(format!(
            "file over folder: expected Already exists, got {other:?}"
        )),
    }
}

#[test]
fn create_rejects_bad_names() -> Result<(), String> {
    let root = dir()?;
    for bad in ["", ".", "..", "a/b", "a\0b"] {
        match create(root.path(), bad, false) {
            Err(message) if message == "Enter one non-empty name, without a slash." => {}
            other => {
                return Err(format!("name {bad:?}: expected name error, got {other:?}"));
            }
        }
    }
    Ok(())
}

#[test]
fn rename_moves_without_replacing() -> Result<(), String> {
    let root = dir()?;
    let before = root.path().join("before.txt");
    write(&before, b"data")?;
    write(&root.path().join("taken.txt"), b"keep")?;
    let moved = rename(&before, "after.txt")?;
    if moved != root.path().join("after.txt") {
        return Err(format!("rename returned an unexpected target: {moved:?}"));
    }
    if before.exists() {
        return Err("rename left the source behind".to_string());
    }
    match rename(&moved, "taken.txt") {
        Err(message) if message == "Already exists: taken.txt" => {}
        other => return Err(format!("expected Already exists, got {other:?}")),
    }
    if read(&root.path().join("taken.txt"))? != b"keep" {
        return Err("rename overwrote the destination".to_string());
    }
    match rename(&moved, "a/b") {
        Err(message) if message == "Enter one non-empty name, without a slash." => Ok(()),
        other => Err(format!("expected name error, got {other:?}")),
    }
}

#[test]
fn remove_file_tree_and_link() -> Result<(), String> {
    let root = dir()?;
    let tree = root.path().join("tree");
    std::fs::create_dir(&tree).map_err(|error| error.to_string())?;
    write(&tree.join("inner.txt"), b"inner")?;
    let link = root.path().join("link");
    std::os::unix::fs::symlink(&tree, &link).map_err(|error| error.to_string())?;
    // Removing a symlink to a directory removes the link, never the target.
    remove(&link)?;
    if link.exists() || link.is_symlink() {
        return Err("symlink was not removed".to_string());
    }
    if !tree.is_dir() {
        return Err("removing a symlink removed its target".to_string());
    }
    remove(&tree)?;
    if tree.exists() {
        return Err("directory tree was not removed".to_string());
    }
    let file = root.path().join("file.txt");
    write(&file, b"x")?;
    remove(&file)?;
    if file.exists() {
        return Err("file was not removed".to_string());
    }
    match remove(&file) {
        Err(_) => Ok(()),
        Ok(()) => Err("removing a missing path should fail".to_string()),
    }
}

#[test]
fn paste_copies_file_with_metadata() -> Result<(), String> {
    let root = dir()?;
    let source_dir = root.path().join("src");
    let dest_dir = root.path().join("dst");
    std::fs::create_dir(&source_dir).map_err(|error| error.to_string())?;
    std::fs::create_dir(&dest_dir).map_err(|error| error.to_string())?;
    let source = source_dir.join("note.txt");
    write(&source, b"hello")?;
    std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o7640))
        .map_err(|error| error.to_string())?;
    let target = paste(&source, &dest_dir, false)?;
    if target != dest_dir.join("note.txt") {
        return Err(format!("paste returned an unexpected target: {target:?}"));
    }
    if read(&target)? != b"hello" {
        return Err("pasted content differs".to_string());
    }
    assert_eq!(target.metadata().unwrap().mode() & 0o7777, 0o1640);
    if !source.exists() {
        return Err("copy removed the source".to_string());
    }
    // Copying again must not overwrite.
    match paste(&source, &dest_dir, false) {
        Err(message) if message == "Already exists: note.txt" => Ok(()),
        other => Err(format!("expected Already exists, got {other:?}")),
    }
}

#[test]
fn paste_copies_file_xattrs_and_times() -> Result<(), String> {
    let root = dir()?;
    let source_dir = root.path().join("src");
    let dest_dir = root.path().join("dst");
    std::fs::create_dir(&source_dir).map_err(|error| error.to_string())?;
    std::fs::create_dir(&dest_dir).map_err(|error| error.to_string())?;
    let source = source_dir.join("note.txt");
    write(&source, b"hello")?;
    std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o640))
        .map_err(|error| error.to_string())?;
    set_times(&source, 1_700_000_000, 1_700_000_123)?;
    let xattrs = try_set_xattr(&source, "user.tdnext-note", b"pinned")?;
    let original = times_of(&source)?;
    let target = paste(&source, &dest_dir, false)?;
    // Times first: reading the copy for the content check below would
    // refresh its access time under relatime.
    if times_of(&target)? != original {
        return Err("pasted times were not preserved".to_string());
    }
    if mode_of(&target)? != 0o640 {
        return Err("pasted mode was not preserved".to_string());
    }
    if read(&target)? != b"hello" {
        return Err("pasted content differs".to_string());
    }
    if xattrs && get_xattr(&target, "user.tdnext-note")? != b"pinned" {
        return Err("pasted xattr value differs".to_string());
    }
    Ok(())
}

#[test]
fn paste_preserves_nested_tree_metadata_and_xattrs() -> Result<(), String> {
    let root = dir()?;
    let source = root.path().join("tree");
    std::fs::create_dir(&source).map_err(|error| error.to_string())?;
    std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o750))
        .map_err(|error| error.to_string())?;
    let nested = source.join("nested");
    std::fs::create_dir(&nested).map_err(|error| error.to_string())?;
    std::fs::set_permissions(&nested, std::fs::Permissions::from_mode(0o750))
        .map_err(|error| error.to_string())?;
    let inner = nested.join("b.txt");
    write(&inner, b"b")?;
    std::fs::set_permissions(&inner, std::fs::Permissions::from_mode(0o640))
        .map_err(|error| error.to_string())?;
    set_times(&inner, 1_700_000_010, 1_700_000_020)?;
    set_times(&nested, 1_700_000_030, 1_700_000_040)?;
    let file_xattrs = try_set_xattr(&inner, "user.tdnext-inner", b"inner")?;
    let dir_xattrs = try_set_xattr(&nested, "user.tdnext-nested", b"nested")?;
    let parent = root.path().join("copy");
    std::fs::create_dir(&parent).map_err(|error| error.to_string())?;
    let original = [times_of(&source)?, times_of(&nested)?, times_of(&inner)?];
    let copied = paste(&source, &parent, false)?;
    // Times before any read: reading a copy refreshes its access time
    // under relatime, and walking the source refreshes the source dirs.
    // The outermost directory keeps source mode/times too (deliberate
    // divergence recorded in `copy-root-metadata.adr.md`).
    if times_of(&copied)? != original[0] {
        return Err("copied root times were not preserved".to_string());
    }
    if times_of(&copied.join("nested"))? != original[1] {
        return Err("nested dir times were not preserved".to_string());
    }
    let copied_nested = copied.join("nested");
    if mode_of(&copied)? != 0o750 {
        return Err("copied root mode was not preserved".to_string());
    }
    if mode_of(&copied_nested)? != 0o750 {
        return Err("nested dir mode was not preserved".to_string());
    }
    let copied_inner = copied_nested.join("b.txt");
    if times_of(&copied_inner)? != original[2] {
        return Err("nested file times were not preserved".to_string());
    }
    if read(&copied_inner)? != b"b" || mode_of(&copied_inner)? != 0o640 {
        return Err("nested file content or mode differs".to_string());
    }
    if file_xattrs && get_xattr(&copied_inner, "user.tdnext-inner")? != b"inner" {
        return Err("nested file xattr was not preserved".to_string());
    }
    if dir_xattrs && get_xattr(&copied_nested, "user.tdnext-nested")? != b"nested" {
        return Err("nested dir xattr was not preserved".to_string());
    }
    Ok(())
}

#[test]
fn paste_copies_symlink_as_symlink() -> Result<(), String> {
    let root = dir()?;
    let dest_dir = root.path().join("dst");
    std::fs::create_dir(&dest_dir).map_err(|error| error.to_string())?;
    let source = root.path().join("link");
    std::os::unix::fs::symlink("note.txt", &source).map_err(|error| error.to_string())?;
    let target = paste(&source, &dest_dir, false)?;
    if std::fs::read_link(&target).map_err(|error| error.to_string())? != Path::new("note.txt") {
        return Err("symlink target was not preserved".to_string());
    }
    if !target.is_symlink() {
        return Err("pasted symlink is not a symlink".to_string());
    }
    Ok(())
}

#[test]
fn paste_refuses_special_files_without_hanging() -> Result<(), String> {
    let root = dir()?;
    let dest_dir = root.path().join("dst");
    std::fs::create_dir(&dest_dir).map_err(|error| error.to_string())?;
    let fifo = root.path().join("pipe");
    make_fifo(&fifo)?;
    match paste(&fifo, &dest_dir, false) {
        Err(message) if message == "Cannot copy special file: pipe" => {}
        other => {
            return Err(format!("expected special-file error, got {other:?}"));
        }
    }
    if dest_dir.join("pipe").exists() || dest_dir.join("pipe").is_symlink() {
        return Err("failed paste left a destination behind".to_string());
    }
    // A tree containing a FIFO is refused before anything is copied.
    let tree = root.path().join("tree");
    std::fs::create_dir(&tree).map_err(|error| error.to_string())?;
    write(&tree.join("ok.txt"), b"ok")?;
    make_fifo(&tree.join("pipe"))?;
    match paste(&tree, &dest_dir, false) {
        Err(message) if message == "Cannot copy special file: pipe" => {}
        other => {
            return Err(format!("expected tree special-file error, got {other:?}"));
        }
    }
    if dest_dir.join("tree").exists() {
        return Err("failed tree paste left a destination behind".to_string());
    }
    Ok(())
}

#[test]
fn paste_refuses_directory_into_itself() -> Result<(), String> {
    let root = dir()?;
    let tree = root.path().join("tree");
    std::fs::create_dir(&tree).map_err(|error| error.to_string())?;
    let inner = tree.join("inner");
    std::fs::create_dir(&inner).map_err(|error| error.to_string())?;
    match paste(&tree, &inner, false) {
        Err(message) if message == "Cannot copy or move a directory into itself." => {}
        other => {
            return Err(format!("expected into-itself error, got {other:?}"));
        }
    }
    match paste(&tree, &tree, true) {
        Err(message) if message == "Cannot copy or move a directory into itself." => Ok(()),
        other => Err(format!("expected into-itself error on move, got {other:?}")),
    }
}

#[test]
fn paste_moves_and_copies_trees() -> Result<(), String> {
    let root = dir()?;
    let source = root.path().join("tree");
    std::fs::create_dir(&source).map_err(|error| error.to_string())?;
    write(&source.join("a.txt"), b"a")?;
    let nested = source.join("nested");
    std::fs::create_dir(&nested).map_err(|error| error.to_string())?;
    write(&nested.join("b.txt"), b"b")?;
    std::os::unix::fs::symlink("a.txt", source.join("link")).map_err(|error| error.to_string())?;
    let copy_parent = root.path().join("copy");
    std::fs::create_dir(&copy_parent).map_err(|error| error.to_string())?;
    let copied = paste(&source, &copy_parent, false)?;
    if read(&copied.join("a.txt"))? != b"a"
        || read(&copied.join("nested").join("b.txt"))? != b"b"
        || !copied.join("link").is_symlink()
    {
        return Err("tree copy is incomplete".to_string());
    }
    if !source.exists() {
        return Err("copy removed the source".to_string());
    }
    let move_parent = root.path().join("moved");
    std::fs::create_dir(&move_parent).map_err(|error| error.to_string())?;
    let moved = paste(&source, &move_parent, true)?;
    if moved != move_parent.join("tree") {
        return Err(format!("move returned an unexpected target: {moved:?}"));
    }
    if source.exists() {
        return Err("move left the source behind".to_string());
    }
    if read(&moved.join("a.txt"))? != b"a" {
        return Err("moved tree content differs".to_string());
    }
    Ok(())
}

#[test]
fn copy_creation_is_private_before_final_metadata() -> Result<(), String> {
    let root = dir()?;
    let folder = root.path().join("private");
    create_private_dir(&folder)?;
    let file = folder.join("file");
    let opened = create_private_file(&file)?;
    assert_eq!(mode_of(&folder)? & 0o077, 0);
    assert_eq!(
        opened.metadata().map_err(|error| error.to_string())?.mode() & 0o177,
        0
    );
    Ok(())
}

#[test]
fn copied_tree_cleanup_removes_only_unchanged_entries() -> Result<(), String> {
    let root = dir()?;
    let source = root.path().join("source");
    std::fs::create_dir(&source).map_err(|error| error.to_string())?;
    let nested = source.join("nested");
    std::fs::create_dir(&nested).map_err(|error| error.to_string())?;
    write(&source.join("a"), b"copied")?;
    write(&nested.join("changed"), b"before")?;
    for p in [&nested, &source] {
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o555))
            .map_err(|e| e.to_string())?;
    }
    let target = root.path().join("target");
    let copied = staged_copy(&source, &target, false)?;
    write(&nested.join("changed"), b"after change")?;
    assert!(cleanup_copied(&copied).is_err());
    assert!(!source.join("a").exists());
    assert_eq!(read(&nested.join("changed"))?, b"after change");
    assert_eq!(read(&target.join("nested/changed"))?, b"before");
    assert_eq!(mode_of(&source)?, 0o555);
    assert_eq!(mode_of(&nested)?, 0o555);
    for path in [&source, &nested, &target, &target.join("nested")] {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[test]
fn copied_tree_cleanup_preserves_new_subtrees() -> Result<(), String> {
    let root = dir()?;
    let source = root.path().join("source");
    std::fs::create_dir(&source).map_err(|error| error.to_string())?;
    let nested = source.join("nested");
    std::fs::create_dir(&nested).map_err(|error| error.to_string())?;
    write(&nested.join("copied"), b"copied")?;
    let copied = staged_copy(&source, &root.path().join("target"), false)?;
    let added = nested.join("new");
    std::fs::create_dir(&added).map_err(|error| error.to_string())?;
    write(&added.join("data"), b"new")?;
    assert!(cleanup_copied(&copied).is_err());
    assert_eq!(read(&added.join("data"))?, b"new");
    assert_eq!(read(&nested.join("copied"))?, b"copied");
    Ok(())
}

#[test]
fn copied_entries_cleanup_handles_files_symlinks_and_unchanged_tree() -> Result<(), String> {
    let root = dir()?;
    let source = root.path().join("source");
    write(&source, b"before")?;
    let copied = staged_copy(&source, &root.path().join("file-copy"), false)?;
    write(&source, b"changed")?;
    assert!(cleanup_copied(&copied).is_err());
    assert_eq!(read(&source)?, b"changed");

    let link = root.path().join("link");
    std::os::unix::fs::symlink("source", &link).map_err(|error| error.to_string())?;
    let copied = staged_copy(&link, &root.path().join("link-copy"), false)?;
    cleanup_copied(&copied)?;
    assert!(!link.is_symlink());
    assert_eq!(read(&source)?, b"changed");

    let tree = root.path().join("tree");
    std::fs::create_dir(&tree).map_err(|error| error.to_string())?;
    std::fs::create_dir(tree.join("nested")).map_err(|error| error.to_string())?;
    write(&tree.join("nested/file"), b"data")?;
    let copied = staged_copy(&tree, &root.path().join("tree-copy"), false)?;
    cleanup_copied(&copied)?;
    assert!(!tree.exists());
    Ok(())
}

#[test]
fn paste_missing_source_reports_gone() -> Result<(), String> {
    let root = dir()?;
    let missing = root.path().join("gone.txt");
    match paste(&missing, root.path(), false) {
        Err(message) if message == "Source no longer exists: gone.txt" => Ok(()),
        other => Err(format!("expected source-gone error, got {other:?}")),
    }
}

#[test]
fn state_dir_resolution_order() -> Result<(), String> {
    let cwd = Path::new("/work/pane");
    type Case<'a> = (Option<&'a str>, Option<&'a str>, Option<&'a str>, &'a str);
    let cases: &[Case<'_>] = &[
        (Some("/state/a"), Some("/xdg"), Some("/home/u"), "/state/a"),
        (None, Some("/xdg"), Some("/home/u"), "/xdg/tdnext"),
        // Blank or relative XDG values are ignored.
        (
            None,
            Some("   "),
            Some("/home/u"),
            "/home/u/.local/share/tdnext",
        ),
        (
            None,
            Some("rel/xdg"),
            Some("/home/u"),
            "/home/u/.local/share/tdnext",
        ),
        (None, None, Some("/home/u"), "/home/u/.local/share/tdnext"),
        // Empty override falls through; relative override joins the cwd.
        (Some(""), Some("/xdg"), Some("/home/u"), "/xdg/tdnext"),
        (Some("rel/state"), None, None, "/work/pane/rel/state"),
    ];
    for (tdnext, xdg, home, expected) in cases {
        let resolved = resolve_state_dir_from(*tdnext, *xdg, *home, Some(cwd));
        if resolved != Path::new(expected) {
            return Err(format!(
                "state dir for ({tdnext:?}, {xdg:?}, {home:?}) is {resolved:?}, expected {expected}"
            ));
        }
    }
    if expanduser("~/bin") == "~/bin" {
        return Err("leading ~/ was not expanded".to_string());
    }
    if expanduser("/abs/path") != "/abs/path" {
        return Err("absolute paths must pass through expanduser".to_string());
    }
    Ok(())
}

#[test]
fn editor_choice_parsing() -> Result<(), String> {
    let path = Path::new("/state/editor.json");
    let cases: &[(&str, EditorChoice)] = &[
        ("{}", EditorChoice::Environment),
        ("{\"editor\": \"environment\"}", EditorChoice::Environment),
        ("{\"editor\": \"nvim\"}", EditorChoice::Nvim),
        ("{\"editor\": \"vi\"}", EditorChoice::Vi),
    ];
    for (text, expected) in cases {
        if parse_editor_choice(text, path)? != *expected {
            return Err(format!("misparsed preferences: {text}"));
        }
    }
    for bad in [
        "{oops",
        "[1, 2]",
        "{\"editor\": \"emacs\"}",
        "{\"editor\": 42}",
        // Explicit null is rejected; only an absent key means the default.
        "{\"editor\": null}",
        "{\"editor\": \"vi\", \"other\": true}",
    ] {
        match parse_editor_choice(bad, path) {
            Err(message)
                if message.starts_with("Cannot read editor preferences /state/editor.json: ") => {}
            other => {
                return Err(format!(
                    "preferences {bad:?}: expected a path-carrying error, got {other:?}"
                ));
            }
        }
    }
    Ok(())
}

#[test]
fn editor_argv_environment_choice() -> Result<(), String> {
    let bin = Path::new("/state/bin");
    let argv = resolve_editor_argv_with(EditorChoice::Environment, None, bin, None)?;
    if argv.len() != 1 || argv[0] != "vi" {
        return Err(format!(
            "default environment editor should be vi, got {argv:?}"
        ));
    }
    let argv = resolve_editor_argv_with(
        EditorChoice::Environment,
        Some("emacs -nw".to_string()),
        bin,
        None,
    )?;
    if argv.len() != 2 || argv[0] != "emacs" || argv[1] != "-nw" {
        return Err(format!("EDITOR was not split, got {argv:?}"));
    }
    match resolve_editor_argv_with(EditorChoice::Environment, Some(String::new()), bin, None) {
        Err(message) if message == "EDITOR is empty; set it to a terminal editor." => {}
        other => return Err(format!("expected empty-EDITOR error, got {other:?}")),
    }
    match resolve_editor_argv_with(
        EditorChoice::Environment,
        Some("\"unterminated".to_string()),
        bin,
        None,
    ) {
        Err(message) if message.starts_with("Cannot start editor: ") => Ok(()),
        other => Err(format!("expected parse error, got {other:?}")),
    }
}

#[test]
fn editor_argv_explicit_choice_prefers_local_bin() -> Result<(), String> {
    let root = dir()?;
    let bin = root.path().join("bin");
    std::fs::create_dir(&bin).map_err(|error| error.to_string())?;
    let local = bin.join("nvim");
    write(&local, b"#!/bin/sh\nexit 0\n")?;
    make_executable(&local)?;
    // Local bin wins even when PATH also provides the binary.
    let other = root.path().join("other-bin");
    std::fs::create_dir(&other).map_err(|error| error.to_string())?;
    let shadow = other.join("nvim");
    write(&shadow, b"#!/bin/sh\nexit 0\n")?;
    make_executable(&shadow)?;
    let other_vi = other.join("vi");
    write(&other_vi, b"#!/bin/sh\nexit 0\n")?;
    make_executable(&other_vi)?;
    let mut path_var = OsString::new();
    path_var.push(&other);
    let argv = resolve_editor_argv_with(EditorChoice::Nvim, None, &bin, Some(&path_var))?;
    if argv.len() != 1 || argv[0] != local.to_string_lossy() {
        return Err(format!("local bin should win, got {argv:?}"));
    }
    // PATH is used when the local bin has no binary.
    let argv = resolve_editor_argv_with(EditorChoice::Vi, None, &bin, Some(&path_var))?;
    if argv.len() != 1 || argv[0] != other_vi.to_string_lossy() {
        return Err(format!("PATH fallback was not used, got {argv:?}"));
    }
    // Nothing installed reports the missing binary without falling back.
    let empty = root.path().join("empty-bin");
    std::fs::create_dir(&empty).map_err(|error| error.to_string())?;
    match resolve_editor_argv_with(EditorChoice::Nvim, None, &empty, Some(OsStr::new(""))) {
        Err(message)
            if message
                == "Editor 'nvim' not found; install it or choose the environment editor." => {}
        other => return Err(format!("expected missing-editor error, got {other:?}")),
    }
    // Non-executable files do not count.
    let blocked = root.path().join("blocked-bin");
    std::fs::create_dir(&blocked).map_err(|error| error.to_string())?;
    write(&blocked.join("nvim"), b"#!/bin/sh\nexit 0\n")?;
    match find_executable_in("nvim", &blocked, Some(OsStr::new(""))) {
        None => Ok(()),
        Some(found) => Err(format!("non-executable file must not resolve: {found}")),
    }
}

#[test]
fn restored_path_planning() -> Result<(), String> {
    // Bootstrapped with an original PATH: restore it.
    if restored_path_plan(Some("1"), Some("1"), Some("/usr/bin:/bin"))
        != RestoredPath::Set("/usr/bin:/bin".to_string())
    {
        return Err("original PATH was not restored".to_string());
    }
    // Bootstrapped with no original PATH (unset): remove it.
    if restored_path_plan(Some("1"), Some("0"), None) != RestoredPath::Remove {
        return Err("absent original PATH was not removed".to_string());
    }
    // Incomplete or forged markers: leave PATH untouched.
    let untouched = [
        (None, None, None),
        (Some("1"), Some("1"), None),
        (Some("1"), Some("2"), Some("/bin")),
        (Some("0"), Some("1"), Some("/bin")),
        (Some("1"), None, Some("/bin")),
    ];
    for (bootstrapped, present, original) in untouched {
        if restored_path_plan(bootstrapped, present, original) != RestoredPath::Keep {
            return Err(format!(
                "markers ({bootstrapped:?}, {present:?}, {original:?}) must leave PATH alone"
            ));
        }
    }
    Ok(())
}

#[test]
fn editor_entry_refuses_non_files() -> Result<(), String> {
    let root = dir()?;
    match editor(root.path()) {
        Err(message) if message == "Select a regular file to edit." => {}
        other => return Err(format!("expected regular-file error, got {other:?}")),
    }
    match editor(&root.path().join("missing.txt")) {
        Err(message) if message == "Select a regular file to edit." => Ok(()),
        other => Err(format!("expected regular-file error, got {other:?}")),
    }
}

#[test]
fn error_surface_stays_strings() -> Result<(), String> {
    // The public API reports failures as plain strings.
    let root = dir()?;
    let missing = root.path().join("gone.txt");
    match rename(&missing, "elsewhere") {
        Err(message) if !message.is_empty() => Ok(()),
        Err(_) => Err("error message must not be empty".to_string()),
        Ok(_) => Err("renaming a missing file should fail".to_string()),
    }
}
#[test]
fn atomic_rename_and_move_reject_preexisting_targets() -> Result<(), String> {
    // Shared owner of the no-overwrite matrix at the atomic boundary.
    // The deleted per-operation public matrices (`create_collision_matrix`,
    // `rename_collision_matrix`, `paste_copy_collision_matrix`,
    // `paste_move_collision_matrix`) all pre-created the rival before calling
    // the public API, so they exercised only the `child_path` pre-check, never
    // the atomic layer. The public smoke tests kept below prove the wiring
    // (`create_never_overwrites`, `rename_moves_without_replacing`,
    // `paste_copies_file_with_metadata` re-copy, and the re-move check in
    // `paste_moves_single_file_and_symlink`); this test bypasses the
    // pre-check and calls the primitives directly with real pre-existing
    // targets, so `RENAME_NOREPLACE` / `O_EXCL` / `mkdir EEXIST` is what
    // rejects. Deterministic: no threads, no timing, no TOCTOU harness.
    let root = dir()?;

    // Folder-over-folder and folder-over-symlink at the primitive: the
    // public `create` pre-check is kind-agnostic, so one primitive case per
    // kind owns the matrix here.
    let docs = root.path().join("docs");
    std::fs::create_dir(&docs).map_err(|error| error.to_string())?;
    match create_private_dir(&docs) {
        Err(message) if message == "Already exists: docs" => {}
        other => {
            return Err(format!(
                "dir over dir: expected Already exists, got {other:?}"
            ));
        }
    }
    write(&root.path().join("real.txt"), b"real")?;
    std::os::unix::fs::symlink("real.txt", root.path().join("link-dir"))
        .map_err(|error| error.to_string())?;
    match create_private_dir(&root.path().join("link-dir")) {
        Err(message) if message == "Already exists: link-dir" => {}
        other => {
            return Err(format!(
                "dir over symlink: expected Already exists, got {other:?}"
            ));
        }
    }
    if !root.path().join("link-dir").is_symlink() {
        return Err("dir creation clobbered a symlink rival".to_string());
    }

    // Rename primitive: every rival kind, source and rival untouched.
    let src_file = root.path().join("rsrc.txt");
    write(&src_file, b"src")?;
    let rival_file = root.path().join("rrival.txt");
    write(&rival_file, b"keep")?;
    match rename_noreplace(&src_file, &rival_file).map_err(|error| error.into_message()) {
        Err(message) if message == "Already exists: rrival.txt" => {}
        other => {
            return Err(format!(
                "rename file over file: expected Already exists, got {other:?}"
            ));
        }
    }
    let rival_dir = root.path().join("rrivaldir");
    std::fs::create_dir(&rival_dir).map_err(|error| error.to_string())?;
    write(&rival_dir.join("keep.txt"), b"keep")?;
    match rename_noreplace(&src_file, &rival_dir).map_err(|error| error.into_message()) {
        Err(message) if message == "Already exists: rrivaldir" => {}
        other => {
            return Err(format!(
                "rename file over dir: expected Already exists, got {other:?}"
            ));
        }
    }
    std::os::unix::fs::symlink("real.txt", root.path().join("rrivallink"))
        .map_err(|error| error.to_string())?;
    match rename_noreplace(&src_file, &root.path().join("rrivallink"))
        .map_err(|error| error.into_message())
    {
        Err(message) if message == "Already exists: rrivallink" => {}
        other => {
            return Err(format!(
                "rename file over symlink: expected Already exists, got {other:?}"
            ));
        }
    }
    if read(&src_file)? != b"src" {
        return Err("failed atomic rename lost the source".to_string());
    }
    let src_dir = root.path().join("rsrcdir");
    std::fs::create_dir(&src_dir).map_err(|error| error.to_string())?;
    write(&src_dir.join("inner.txt"), b"inner")?;
    let rival_dir2 = root.path().join("rrivaldir2");
    std::fs::create_dir(&rival_dir2).map_err(|error| error.to_string())?;
    write(&rival_dir2.join("keep.txt"), b"keep")?;
    match rename_noreplace(&src_dir, &rival_dir2).map_err(|error| error.into_message()) {
        Err(message) if message == "Already exists: rrivaldir2" => {}
        other => {
            return Err(format!(
                "rename dir over dir: expected Already exists, got {other:?}"
            ));
        }
    }
    write(&root.path().join("rrivalfile"), b"keep")?;
    match rename_noreplace(&src_dir, &root.path().join("rrivalfile"))
        .map_err(|error| error.into_message())
    {
        Err(message) if message == "Already exists: rrivalfile" => {}
        other => {
            return Err(format!(
                "rename dir over file: expected Already exists, got {other:?}"
            ));
        }
    }
    if read(&src_dir.join("inner.txt"))? != b"inner"
        || read(&rival_dir.join("keep.txt"))? != b"keep"
        || read(&rival_dir2.join("keep.txt"))? != b"keep"
    {
        return Err("atomic rename altered source or rival".to_string());
    }

    // Move primitive (same-device `RENAME_NOREPLACE` path): refused moves
    // keep both sides.
    let msrc = root.path().join("msrc.txt");
    write(&msrc, b"src")?;
    let mdst = root.path().join("mdst.txt");
    write(&mdst, b"keep")?;
    match move_noreplace(&msrc, &mdst) {
        Err(message) if message == "Already exists: mdst.txt" => {}
        other => {
            return Err(format!(
                "move file over file: expected Already exists, got {other:?}"
            ));
        }
    }
    if read(&msrc)? != b"src" || read(&mdst)? != b"keep" {
        return Err("atomic move altered source or rival".to_string());
    }
    let msrcdir = root.path().join("msrcdir");
    std::fs::create_dir(&msrcdir).map_err(|error| error.to_string())?;
    write(&msrcdir.join("a.txt"), b"src")?;
    let mdstdir = root.path().join("mdstdir");
    std::fs::create_dir(&mdstdir).map_err(|error| error.to_string())?;
    write(&mdstdir.join("keep.txt"), b"keep")?;
    match move_noreplace(&msrcdir, &mdstdir) {
        Err(message) if message == "Already exists: mdstdir" => {}
        other => {
            return Err(format!(
                "move dir over dir: expected Already exists, got {other:?}"
            ));
        }
    }
    if read(&msrcdir.join("a.txt"))? != b"src" || read(&mdstdir.join("keep.txt"))? != b"keep" {
        return Err("atomic dir move altered source or rival".to_string());
    }
    std::os::unix::fs::symlink("real.txt", root.path().join("mlink"))
        .map_err(|error| error.to_string())?;
    write(&root.path().join("mkeep"), b"keep")?;
    match move_noreplace(&root.path().join("mlink"), &root.path().join("mkeep")) {
        Err(message) if message == "Already exists: mkeep" => {}
        other => {
            return Err(format!(
                "move symlink over file: expected Already exists, got {other:?}"
            ));
        }
    }
    if std::fs::read_link(root.path().join("mlink")).map_err(|error| error.to_string())?
        != Path::new("real.txt")
        || read(&root.path().join("mkeep"))? != b"keep"
    {
        return Err("atomic symlink move altered source or rival".to_string());
    }
    Ok(())
}

#[test]
fn paste_moves_single_file_and_symlink() -> Result<(), String> {
    // `paste_moves_and_copies_trees` covers tree moves; Python
    // `test_copy_and_move_real_files` also covers single-file moves, which
    // take the same-device `RENAME_NOREPLACE` path and must preserve content
    // and remove the source. Symlink moves rename the link itself.
    let root = dir()?;
    let src_dir = root.path().join("src");
    let dst_dir = root.path().join("dst");
    std::fs::create_dir(&src_dir).map_err(|error| error.to_string())?;
    std::fs::create_dir(&dst_dir).map_err(|error| error.to_string())?;
    let source = src_dir.join("note.txt");
    write(&source, b"hello")?;
    std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o640))
        .map_err(|error| error.to_string())?;
    let target = paste(&source, &dst_dir, true)?;
    if target != dst_dir.join("note.txt") {
        return Err(format!("move returned an unexpected target: {target:?}"));
    }
    if source.exists() || source.is_symlink() {
        return Err("move left the source behind".to_string());
    }
    if read(&target)? != b"hello" {
        return Err("moved content differs".to_string());
    }
    if mode_of(&target)? != 0o640 {
        return Err("moved mode was not preserved".to_string());
    }
    write(&src_dir.join("real.txt"), b"real")?;
    let link = src_dir.join("link");
    std::os::unix::fs::symlink("real.txt", &link).map_err(|error| error.to_string())?;
    let moved_link = paste(&link, &dst_dir, true)?;
    if std::fs::read_link(&moved_link).map_err(|error| error.to_string())? != Path::new("real.txt")
    {
        return Err("moved symlink target differs".to_string());
    }
    if link.exists() || link.is_symlink() {
        return Err("symlink move left the source behind".to_string());
    }
    if !moved_link.is_symlink() {
        return Err("moved symlink is not a symlink".to_string());
    }
    write(&src_dir.join("second.txt"), b"second")?;
    write(&dst_dir.join("second.txt"), b"keep")?;
    match paste(&src_dir.join("second.txt"), &dst_dir, true) {
        Err(message) if message == "Already exists: second.txt" => {}
        other => {
            return Err(format!(
                "move file over file: expected Already exists, got {other:?}"
            ));
        }
    }
    if read(&src_dir.join("second.txt"))? != b"second"
        || read(&dst_dir.join("second.txt"))? != b"keep"
    {
        return Err("public move collision altered source or rival".to_string());
    }
    Ok(())
}

#[test]
fn paste_special_files_socket_and_move_tree() -> Result<(), String> {
    use std::os::unix::fs::FileTypeExt as _;
    // Sockets are special files like FIFOs (Python `_ensure_tree_copyable`
    // and `_copy_file_noreplace` refuse both). The existing
    // `paste_refuses_special_files_without_hanging` covers FIFO copy; this
    // covers sockets plus the move-side tree pre-check that Python tests in
    // `test_paste_move_directory_with_fifo_keeps_source`.
    let root = dir()?;
    let dst_dir = root.path().join("dst");
    std::fs::create_dir(&dst_dir).map_err(|error| error.to_string())?;
    let sock = root.path().join("sock");
    let _listener =
        std::os::unix::net::UnixListener::bind(&sock).map_err(|error| error.to_string())?;
    match paste(&sock, &dst_dir, false) {
        Err(message) if message == "Cannot copy special file: sock" => {}
        other => {
            return Err(format!(
                "socket copy: expected special-file error, got {other:?}"
            ));
        }
    }
    if dst_dir.join("sock").exists()
        || dst_dir.join("sock").is_symlink()
        || std::fs::symlink_metadata(dst_dir.join("sock")).is_ok()
    {
        return Err("failed socket paste left a destination behind".to_string());
    }
    // Moving a tree that contains a FIFO is refused before anything is
    // copied or renamed; the source tree stays intact.
    let tree = root.path().join("srcdir");
    std::fs::create_dir(&tree).map_err(|error| error.to_string())?;
    write(&tree.join("ok.txt"), b"ok")?;
    make_fifo(&tree.join("pipe"))?;
    match paste(&tree, &dst_dir, true) {
        Err(message) if message == "Cannot copy special file: pipe" => {}
        other => {
            return Err(format!(
                "move tree with fifo: expected special-file error, got {other:?}"
            ));
        }
    }
    if dst_dir.join("srcdir").exists() {
        return Err("failed tree move left a destination behind".to_string());
    }
    if read(&tree.join("ok.txt"))? != b"ok" {
        return Err("failed tree move altered the source".to_string());
    }
    // A lone FIFO moved on the same filesystem renames without opening it,
    // so it cannot hang; cross-device copies of the same source are refused
    // by the copy path (Python `test_paste_move_fifo_cross_device_fails_safe`
    // forces `EXDEV` to reach that path, which has no public-API trigger
    // here and stays covered by the copy refusal above).
    let fifo = root.path().join("lone-pipe");
    make_fifo(&fifo)?;
    let moved = paste(&fifo, &dst_dir, true)?;
    if fifo.exists() || std::fs::symlink_metadata(&fifo).is_ok() {
        return Err("fifo move left the source behind".to_string());
    }
    let moved_type = std::fs::symlink_metadata(&moved)
        .map_err(|error| error.to_string())?
        .file_type();
    if !moved_type.is_fifo() {
        return Err("moved fifo is not a fifo".to_string());
    }
    Ok(())
}

#[test]
fn cross_device_fifo_move_preserves_source_and_creates_no_destination() -> Result<(), String> {
    use std::os::unix::fs::FileTypeExt as _;

    let source_dir = dir()?;
    let destination = tempfile::TempDir::new_in("/dev/shm").map_err(|error| error.to_string())?;
    let source_device = source_dir
        .path()
        .metadata()
        .map_err(|error| error.to_string())?
        .dev();
    let destination_device = destination
        .path()
        .metadata()
        .map_err(|error| error.to_string())?
        .dev();
    assert_ne!(
        source_device, destination_device,
        "test requires distinct devices"
    );
    let fifo = source_dir.path().join("pipe");
    make_fifo(&fifo)?;
    let (tx, rx) = std::sync::mpsc::channel();
    let source = fifo.clone();
    let dest = destination.path().to_path_buf();
    std::thread::spawn(move || tx.send(paste(&source, &dest, true)).unwrap());
    let result = rx
        .recv_timeout(std::time::Duration::from_secs(2))
        .map_err(|e| e.to_string())?;
    assert!(result.is_err(), "cross-device FIFO move must fail");
    assert!(
        std::fs::symlink_metadata(&fifo)
            .map_err(|e| e.to_string())?
            .file_type()
            .is_fifo()
    );
    assert!(std::fs::symlink_metadata(destination.path().join("pipe")).is_err());
    Ok(())
}

#[test]
fn atomic_create_file_rejects_existing_file_and_symlinks() -> Result<(), String> {
    let root = dir()?;
    let real = root.path().join("real");
    write(&real, b"keep")?;
    let link = root.path().join("link");
    let dangling = root.path().join("dangling");
    std::os::unix::fs::symlink("real", &link).map_err(|e| e.to_string())?;
    std::os::unix::fs::symlink("missing", &dangling).map_err(|e| e.to_string())?;
    for rival in [&real, &link, &dangling] {
        assert!(create_private_file(rival).is_err());
    }
    assert_eq!(read(&real)?, b"keep");
    assert_eq!(
        std::fs::read_link(&link).map_err(|e| e.to_string())?,
        Path::new("real")
    );
    assert_eq!(
        std::fs::read_link(&dangling).map_err(|e| e.to_string())?,
        Path::new("missing")
    );
    Ok(())
}

#[test]
fn atomic_copy_rejects_existing_file_directory_and_symlink_destinations() -> Result<(), String> {
    let root = dir()?;
    let source = root.path().join("source");
    write(&source, b"source")?;
    let rival = root.path().join("rival");
    write(&rival, b"keep")?;
    assert!(staged_copy(&source, &rival, false).is_err());
    let link = root.path().join("link");
    std::os::unix::fs::symlink("rival", &link).map_err(|e| e.to_string())?;
    assert!(staged_copy(&source, &link, false).is_err());
    let source_tree = root.path().join("tree");
    std::fs::create_dir(&source_tree).map_err(|e| e.to_string())?;
    write(&source_tree.join("source"), b"tree")?;
    let rival_tree = root.path().join("existing-tree");
    std::fs::create_dir(&rival_tree).map_err(|e| e.to_string())?;
    write(&rival_tree.join("keep"), b"rival")?;
    assert!(staged_copy(&source_tree, &rival_tree, false).is_err());
    assert_eq!(read(&source)?, b"source");
    assert_eq!(read(&rival)?, b"keep");
    assert_eq!(
        std::fs::read_link(&link).map_err(|e| e.to_string())?,
        Path::new("rival")
    );
    assert_eq!(read(&source_tree.join("source"))?, b"tree");
    assert_eq!(read(&rival_tree.join("keep"))?, b"rival");
    Ok(())
}

#[test]
fn copied_tree_drops_inherited_acls() -> Result<(), Box<dyn std::error::Error>> {
    let root = dir()?;
    let tree = root.path().join("src");
    let dst = root.path().join("dst");
    std::fs::create_dir(&tree)?;
    std::fs::create_dir(&dst)?;
    write(&tree.join("file"), b"data")?;
    // Linux POSIX ACL xattr, granting an additional named user read access.
    let mut acl = 2u32.to_le_bytes().to_vec();
    for (tag, perm, id) in [
        (1u16, 7u16, u32::MAX),
        (2, 4, 65534),
        (4, 0, u32::MAX),
        (16, 4, u32::MAX),
        (32, 0, u32::MAX),
    ] {
        acl.extend(tag.to_le_bytes());
        acl.extend(perm.to_le_bytes());
        acl.extend(id.to_le_bytes());
    }
    match std::process::Command::new("setfacl")
        .args(["-m", "d:u:65534:r--"])
        .arg(&dst)
        .status()
    {
        Ok(status) => assert!(status.success()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => rustix::fs::setxattr(
            &dst,
            "system.posix_acl_default",
            &acl,
            rustix::fs::XattrFlags::empty(),
        )?,
        Err(e) => return Err(e.into()),
    }
    let result = paste(&tree, &dst, false)?;
    for (path, attr) in [
        (&result, "system.posix_acl_default"),
        (&result, "system.posix_acl_access"),
        (&result.join("file"), "system.posix_acl_access"),
    ] {
        assert_eq!(
            rustix::fs::getxattr_query(path, attr),
            Err(rustix::io::Errno::NODATA)
        );
    }
    Ok(())
}

#[test]
fn copy_separates_linked_names_while_cross_device_move_preserves_them()
-> Result<(), Box<dyn std::error::Error>> {
    let root = dir()?;
    let dst = tempfile::tempdir_in("/dev/shm")?;
    let tree = root.path().join("src");
    std::fs::create_dir(&tree)?;
    write(&tree.join("a"), b"linked")?;
    std::fs::hard_link(tree.join("a"), tree.join("b"))?;
    let copy_dir = dir()?;
    let copy = paste(&tree, copy_dir.path(), false)?;
    write(&copy.join("a"), b"edited")?;
    assert_eq!(read(&copy.join("b"))?, b"linked");
    let target = paste(&tree, dst.path(), true)?;
    assert!(!tree.exists());
    assert_eq!(read(&target.join("b"))?, b"linked");
    assert_eq!(
        target.join("a").metadata()?.ino(),
        target.join("b").metadata()?.ino()
    );
    Ok(())
}

#[test]
fn nested_bind_mount_blocks_all_unlinks() -> Result<(), Box<dyn std::error::Error>> {
    // Probe the live namespace read-only; never run deletion against /proc.
    let proc = Path::new("/proc");
    let root = std::fs::File::open("/")?;
    use std::os::fd::AsRawFd;
    let fdinfo = std::fs::read_to_string(format!("/proc/self/fdinfo/{}", root.as_raw_fd()))?;
    let kernel_id: u64 = fdinfo
        .lines()
        .find_map(|l| l.strip_prefix("mnt_id:"))
        .unwrap()
        .trim()
        .parse()?;
    assert_eq!(crate::actions::mount(&root)?.1, kernel_id);
    assert_ne!(
        crate::actions::mount(&root)?.1,
        crate::actions::mount(&std::fs::File::open(proc)?)?.1
    );
    assert!(
        crate::actions::checked_dir(&root, proc, crate::actions::identity(proc)?)
            .unwrap_err()
            .contains("mount point: /proc")
    );
    let tmp = dir()?;
    let tree = tmp.path().join("tree");
    let nested = tree.join("nested/bind");
    std::fs::create_dir_all(&nested)?;
    write(&nested.join("inside"), b"mounted")?;
    write(&tree.join("beside"), b"outside")?;
    let m = nested.metadata()?;
    crate::actions::BIND_ROOT.set(Some((m.dev(), m.ino())));
    crate::actions::BEFORE_CLAIM.set(Some(Box::new(|_| {
        panic!("mount preflight must finish before any claim")
    })));
    let error = remove(&tree).unwrap_err();
    assert_eq!(
        error,
        format!("Cannot remove mount point: {}", nested.display())
    );
    assert_eq!(read(&tree.join("beside"))?, b"outside");
    assert_eq!(read(&nested.join("inside"))?, b"mounted");
    let m = nested.join("inside").metadata()?;
    crate::actions::BIND_ROOT.set(Some((m.dev(), m.ino())));
    assert!(
        remove(&tree)
            .unwrap_err()
            .contains(nested.join("inside").to_str().unwrap())
    );
    assert_eq!(read(&tree.join("beside"))?, b"outside");
    Ok(())
}

#[test]
fn late_rival_and_foreign_staging_entry_survive_failed_commit()
-> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = tempfile::tempdir_in("/dev/shm")?;
    let tree = src.path().join("tree");
    std::fs::create_dir(&tree)?;
    write(&tree.join("payload"), b"old")?;
    let target = dst.path().join("tree");
    let rival = target.clone();
    crate::actions::stash_between_note(&target, move || {
        let stage = std::fs::read_dir(rival.parent().unwrap())
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        std::fs::create_dir(&rival).unwrap();
        std::fs::write(rival.join("keep"), b"rival").unwrap();
        std::fs::write(stage.join("tree/foreign"), b"foreign").unwrap();
    });
    let err = paste(&tree, dst.path(), true).unwrap_err();
    assert!(err.contains("Staging remains"), "{err}");
    assert_eq!(read(&tree.join("payload"))?, b"old");
    assert_eq!(read(&target.join("keep"))?, b"rival");
    let stage = std::fs::read_dir(dst.path())?
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .find(|e| e.path() != target)
        .unwrap()
        .path();
    assert!(err.contains(stage.to_str().unwrap()));
    assert_eq!(read(&stage.join("tree/foreign"))?, b"foreign");
    assert!(!stage.join("tree/payload").exists());
    Ok(())
}

#[test]
fn copy_accepts_destination_timestamp_rounding() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = dir()?;
    let file = src.path().join("file");
    write(&file, b"content")?;
    set_times(&file, 1700000001, 1700000001)?;
    let parent = dst.path().to_owned();
    crate::actions::stash_between_note(&parent.join("file"), move || {
        let stage = std::fs::read_dir(parent)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        set_times(&stage.join("file"), 1700000000, 1700000000).unwrap();
    });
    let copy = paste(&file, dst.path(), false)?;
    assert_eq!(copy.metadata()?.mtime(), 1700000000);
    assert_eq!(read(&copy)?, b"content");
    Ok(())
}

#[test]
fn moved_source_survives_replacement_of_published_path() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = tempfile::tempdir_in("/dev/shm")?;
    let file = src.path().join("a");
    write(&file, b"original")?;
    let target = dst.path().join("a");
    let path = target.clone();
    crate::actions::stash_between_note(&file, move || {
        std::fs::rename(&path, path.with_file_name("published")).unwrap();
        std::fs::write(path, b"replacement").unwrap();
    });
    assert!(paste(&file, dst.path(), true).is_err());
    assert_eq!(read(&file)?, b"original");
    assert_eq!(read(&target)?, b"replacement");
    Ok(())
}
#[test]
fn staging_replacement_is_not_owned_by_copy() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = dir()?;
    let file = src.path().join("a");
    write(&file, b"original")?;
    crate::actions::BEFORE_RECORD.set(Some(Box::new(|p| {
        std::fs::remove_file(p).unwrap();
        std::fs::write(p, b"replacement").unwrap();
    })));
    let err = paste(&file, dst.path(), false).unwrap_err();
    assert!(err.contains("Staging remains"), "{err}");
    let stage = std::fs::read_dir(dst.path())?.next().unwrap()?.path();
    assert_eq!(read(&stage.join("a"))?, b"replacement");
    Ok(())
}
#[test]
fn confirmed_unlink_restores_rival_claimed_after_check() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let file = src.path().join("a");
    write(&file, b"original")?;
    crate::actions::BEFORE_CLAIM.set(Some(Box::new(|p| {
        std::fs::remove_file(p).unwrap();
        std::fs::write(p, b"replacement").unwrap();
    })));
    assert!(remove(&file).is_err());
    assert_eq!(read(&file)?, b"replacement");
    Ok(())
}
#[test]
fn late_regular_file_wins_publish_race() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = dir()?;
    let file = src.path().join("a");
    write(&file, b"original")?;
    let target = dst.path().join("a");
    let rival = target.clone();
    crate::actions::stash_between_note(&target, move || std::fs::write(rival, b"rival").unwrap());
    assert!(paste(&file, dst.path(), false).is_err());
    assert_eq!(read(&target)?, b"rival");
    Ok(())
}
#[test]
fn setgid_destination_does_not_gain_source_group_permissions()
-> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = dir()?;
    let file = src.path().join("a");
    write(&file, b"private")?;
    let gid = file.metadata()?.gid();
    let groups = std::process::Command::new("id").arg("-G").output()?;
    if let Some(other) = std::str::from_utf8(&groups.stdout)?
        .split_whitespace()
        .flat_map(str::parse::<u32>)
        .find(|g| *g != gid)
    {
        std::os::unix::fs::chown(dst.path(), None, Some(other))?;
        std::fs::set_permissions(dst.path(), std::fs::Permissions::from_mode(0o2770))?;
        let copy = paste(&file, dst.path(), false)?;
        assert_eq!(copy.metadata()?.gid(), gid);
    }
    // An unassignable source group exercises the other branch even when
    // the test runner belongs to only one group. Root may assign the GID.
    let group = 123454321;
    crate::actions::TEST_SOURCE_GROUP.set(Some(group));
    let restricted = src.path().join("restricted");
    write(&restricted, b"data")?;
    std::fs::set_permissions(&restricted, std::fs::Permissions::from_mode(0o604))?;
    let restricted = paste(&restricted, dst.path(), false)?;
    if restricted.metadata()?.gid() != group {
        assert_eq!(mode_of(&restricted)?, 0o600);
    }
    let folder = src.path().join("folder");
    std::fs::create_dir(&folder)?;
    let mut acl = 2u32.to_le_bytes().to_vec();
    for (tag, id) in [
        (1u16, u32::MAX),
        (4, u32::MAX),
        (8, 65534),
        (16, u32::MAX),
        (32, u32::MAX),
    ] {
        acl.extend(tag.to_le_bytes());
        acl.extend(
            match tag {
                1 | 16 => 7u16,
                32 => 4,
                _ => 5,
            }
            .to_le_bytes(),
        );
        acl.extend(id.to_le_bytes());
    }
    for key in ["system.posix_acl_access", "system.posix_acl_default"] {
        rustix::fs::setxattr(&folder, key, &acl, rustix::fs::XattrFlags::empty())?;
    }
    let copy = paste(&folder, dst.path(), false)?;
    crate::actions::TEST_SOURCE_GROUP.set(None);
    if copy.metadata()?.gid() != group {
        assert_eq!(copy.metadata()?.mode() & 0o077, 0o44);
        for key in ["system.posix_acl_access", "system.posix_acl_default"] {
            let stored = get_xattr(&copy, key)?;
            for record in stored[4..].chunks_exact(8) {
                if matches!(u16::from_le_bytes([record[0], record[1]]), 4 | 8 | 16 | 32) {
                    assert_eq!(&record[2..4], &[4, 0]);
                }
            }
        }
    }
    Ok(())
}

fn transfer_read_only_tree(moving: bool) -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = if moving {
        tempfile::tempdir_in("/dev/shm")?
    } else {
        dir()?
    };
    let tree = src.path().join("readonly");
    let nested = tree.join("nested");
    std::fs::create_dir_all(&nested)?;
    write(&nested.join("file"), b"payload")?;
    if moving {
        assert_ne!(tree.metadata()?.dev(), dst.path().metadata()?.dev());
    }
    for (path, stamp) in [(&tree, 1600000010), (&nested, 1600000020)] {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o555))?;
        set_times(path, stamp, stamp + 1)?;
    }
    let target = paste(&tree, dst.path(), moving)?;
    for (path, stamp) in [(&target, 1600000010), (&target.join("nested"), 1600000020)] {
        assert_eq!(mode_of(path)?, 0o555);
        assert_eq!(times_of(path)?, (stamp, 0, stamp + 1, 0));
    }
    assert_eq!(read(&target.join("nested/file"))?, b"payload");
    assert_eq!(tree.exists(), !moving);
    for root in [target, tree].into_iter().filter(|root| root.exists()) {
        for path in [root.clone(), root.join("nested")] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))?;
        }
    }
    Ok(())
}

#[test]
fn copy_read_only_tree() -> Result<(), Box<dyn std::error::Error>> {
    transfer_read_only_tree(false)
}

#[test]
fn move_read_only_tree_across_devices() -> Result<(), Box<dyn std::error::Error>> {
    transfer_read_only_tree(true)
}

#[test]
fn interrupted_delete_keeps_remaining_children_visible() -> Result<(), Box<dyn std::error::Error>> {
    let tmp = dir()?;
    let tree = tmp.path().join("tree");
    std::fs::create_dir(&tree)?;
    for n in ["a", "b"] {
        write(&tree.join(n), b"keep")?;
    }
    crate::actions::AFTER_UNLINK.set(Some(Box::new(|_| panic!("interrupt after first unlink"))));
    assert!(std::panic::catch_unwind(|| remove(&tree)).is_err());
    assert_eq!(std::fs::read_dir(&tree)?.count(), 1);
    Ok(())
}
#[test]
fn missing_published_child_preserves_its_source_only() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = tempfile::tempdir_in("/dev/shm")?;
    let tree = src.path().join("tree");
    std::fs::create_dir(&tree)?;
    for n in ["a", "b"] {
        write(&tree.join(n), b"original")?;
    }
    let missing = dst.path().join("tree/a");
    crate::actions::stash_between_note(&tree, move || std::fs::remove_file(missing).unwrap());
    let err = paste(&tree, dst.path(), true).unwrap_err();
    assert!(err.contains("Partial move"));
    assert_eq!(read(&tree.join("a"))?, b"original");
    assert!(!tree.join("b").exists());
    Ok(())
}
fn replaced_stage_entry(directory: bool) -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = dir()?;
    let item = src.path().join("item");
    if directory {
        std::fs::create_dir(&item)?;
    } else {
        std::os::unix::fs::symlink("original", &item)?;
    }
    crate::actions::BEFORE_RECORD.set(Some(Box::new(move |p| {
        std::fs::rename(p, p.with_file_name("displaced")).unwrap();
        if directory {
            std::fs::create_dir(p).unwrap();
        } else {
            std::os::unix::fs::symlink("replaced", p).unwrap();
        }
    })));
    let err = paste(&item, dst.path(), false).unwrap_err();
    assert!(err.contains("Staging remains"), "{err}");
    assert!(!dst.path().join("item").exists());
    let stage = std::fs::read_dir(dst.path())?.next().unwrap()?.path();
    if directory {
        assert!(stage.join("item").is_dir());
    } else {
        assert_eq!(
            std::fs::read_link(stage.join("item"))?,
            Path::new("replaced")
        );
    }
    Ok(())
}
#[test]
fn staged_directory_replacement_survives_rollback() -> Result<(), Box<dyn std::error::Error>> {
    replaced_stage_entry(true)
}
#[test]
fn staged_symlink_replacement_survives_rollback() -> Result<(), Box<dyn std::error::Error>> {
    replaced_stage_entry(false)
}
#[test]
fn same_length_staged_edit_is_not_published() -> Result<(), Box<dyn std::error::Error>> {
    let src = dir()?;
    let dst = dir()?;
    let item = src.path().join("file");
    write(&item, b"good")?;
    let parent = dst.path().to_owned();
    crate::actions::stash_between_note(&parent.join("file"), move || {
        let stage = std::fs::read_dir(parent)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        std::fs::write(stage.join("file"), b"evil").unwrap();
    });
    assert!(paste(&item, dst.path(), false).is_err());
    assert!(!dst.path().join("file").exists());
    Ok(())
}
