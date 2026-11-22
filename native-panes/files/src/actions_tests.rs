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
    EditorChoice, RestoredPath, create, editor, expanduser, find_executable_in,
    parse_editor_choice, paste, remove, rename, resolve_editor_argv_with, resolve_state_dir_from,
    restored_path_plan,
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
    std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o640))
        .map_err(|error| error.to_string())?;
    let target = paste(&source, &dest_dir, false)?;
    if target != dest_dir.join("note.txt") {
        return Err(format!("paste returned an unexpected target: {target:?}"));
    }
    if read(&target)? != b"hello" {
        return Err("pasted content differs".to_string());
    }
    if mode_of(&target)? != 0o640 {
        return Err(format!(
            "pasted mode is {:o}, expected 640",
            mode_of(&target)?
        ));
    }
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
        ("{\"editor\": null}", EditorChoice::Environment),
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
