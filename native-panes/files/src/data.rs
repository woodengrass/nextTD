use chrono::{Local, TimeZone};
use std::{
    fs,
    io::{self, Read},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};
use unicase::UniCase;
use unicode_general_category::{GeneralCategory as G, get_general_category};

#[derive(Default, Debug)]
pub struct Entry {
    pub path: PathBuf,
    pub directory: bool,
    pub size: String,
    pub modified: String,
}
#[derive(Default, Debug)]
pub struct Listing {
    pub entries: Vec<Entry>,
    pub neighbours: Vec<PathBuf>,
    pub error: String,
    pub neighbour_error: String,
}
#[derive(Default, Debug)]
pub struct Preview {
    pub meta: Vec<String>,
    pub lines: Vec<String>,
}

pub fn single_line(s: impl AsRef<std::ffi::OsStr>) -> String {
    use std::{fmt::Write as _, os::unix::ffi::OsStrExt as _};
    let mut out = String::new();
    for chunk in s.as_ref().as_bytes().utf8_chunks() {
        for c in chunk.valid().chars() {
            match get_general_category(c) {
                _ if c == '\\' => out.push_str("\\\\"),
                G::Control
                | G::Format
                | G::Surrogate
                | G::PrivateUse
                | G::Unassigned
                | G::LineSeparator
                | G::ParagraphSeparator
                | G::SpaceSeparator
                    if c != ' ' =>
                {
                    write!(out, "\\u{{{:x}}}", c as u32).unwrap();
                }
                _ => out.push(c),
            }
        }
        for b in chunk.invalid() {
            write!(out, "\\x{b:02x}").unwrap();
        }
    }
    out
}
fn bytes(size: u64) -> String {
    let digits = size.to_string();
    let mut s = String::new();
    for (i, c) in digits.char_indices() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            s.push(',')
        }
        s.push(c)
    }
    s.push_str(" B");
    s
}
fn size_label(size: u64) -> String {
    let exact = bytes(size);
    if exact.len() <= 9 {
        return exact;
    }
    let mut v = size as f64;
    for unit in ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB"] {
        v /= 1024.;
        if v < 1024. {
            return if v >= 100. {
                format!("{v:.0} {unit}")
            } else {
                format!("{v:.1} {unit}")
            };
        }
    }
    exact
}
fn mtime(m: &fs::Metadata) -> String {
    Local
        .timestamp_opt(m.mtime(), 0)
        .single()
        .map_or_else(|| "–".into(), |d| d.format("%Y-%m-%d %H:%M").to_string())
}
fn paths(dir: &Path, hidden: bool) -> io::Result<impl Iterator<Item = io::Result<PathBuf>>> {
    Ok(fs::read_dir(dir)?.filter_map(move |e| match e {
        Ok(e) if !hidden && e.file_name().as_encoded_bytes().starts_with(b".") => None,
        r => Some(r.map(|e| e.path())),
    }))
}
fn sort_name(p: &Path) -> (UniCase<String>, std::ffi::OsString) {
    let name = p.file_name().unwrap_or_default();
    (
        UniCase::new(name.to_string_lossy().into_owned()),
        name.to_owned(),
    )
}
pub fn list(dir: &Path, hidden: bool) -> Listing {
    let scan = || -> io::Result<Vec<Entry>> {
        let mut out = Vec::new();
        for p in paths(dir, hidden)? {
            let path = p?;
            let meta = fs::symlink_metadata(&path).ok();
            let directory = meta
                .as_ref()
                .is_some_and(|m| m.is_dir() || m.is_symlink() && path.is_dir());
            let size = meta
                .as_ref()
                .filter(|_| !directory)
                .map_or_else(|| "–".into(), |m| size_label(m.len()));
            let modified = meta.as_ref().map_or_else(|| "–".into(), mtime);
            out.push(Entry {
                path,
                directory,
                size,
                modified,
            })
        }
        out.sort_by_cached_key(|e| (!e.directory, sort_name(&e.path)));
        Ok(out)
    };
    let entries = match scan() {
        Ok(v) => v,
        Err(e) => {
            return Listing {
                error: format!("Cannot read directory: {}", os_error(&e, dir)),
                ..Listing::default()
            };
        }
    };
    let mut out = Listing {
        entries,
        ..Listing::default()
    };
    if let Some(parent) = dir.parent() {
        let siblings = || -> io::Result<Vec<PathBuf>> {
            let mut v = Vec::new();
            for p in paths(parent, hidden)? {
                let p = p?;
                if p.is_dir() {
                    v.push(p)
                }
            }
            Ok(v)
        };
        match siblings() {
            Ok(v) => out.neighbours = v,
            Err(e) => out.neighbour_error = e.to_string(),
        }
    }
    if !out.neighbours.iter().any(|p| p == dir) {
        out.neighbours.push(dir.to_owned())
    }
    out.neighbours.sort_by_cached_key(|p| sort_name(p));
    out
}
fn os_error(e: &io::Error, path: &Path) -> String {
    let Some(n) = e.raw_os_error() else {
        return e.to_string();
    };
    let text = e.to_string();
    format!(
        "[Errno {n}] {}: '{}'",
        text.split(" (os error").next().unwrap_or(&text),
        path.display()
    )
}

#[cfg(test)]
thread_local! {
    static BEFORE_PREVIEW_OPEN: std::cell::RefCell<Option<Box<dyn FnOnce()>>> =
        std::cell::RefCell::new(None);
}

pub fn preview(path: &Path) -> Preview {
    let name = single_line(path.file_name().unwrap_or_default());
    let make = |kind: &str,
                size: Option<u64>,
                meta: Option<&fs::Metadata>,
                short: bool,
                lines: Vec<String>| Preview {
        meta: vec![
            name.clone(),
            format!(
                "{kind}  ·  {}{}",
                size.map_or_else(|| "–".into(), bytes),
                if short { "  ·  truncated" } else { "" }
            ),
            format!("Modified {}", meta.map_or_else(|| "–".into(), mtime)),
        ],
        lines,
    };
    let read = || -> io::Result<Preview> {
        let m = fs::symlink_metadata(path)?;
        if m.is_symlink() {
            return Ok(make(
                "Link",
                Some(m.len()),
                Some(&m),
                false,
                vec![single_line(format!(
                    "Link to {}",
                    fs::read_link(path)?.display()
                ))],
            ));
        }
        if m.is_dir() {
            return Ok(make(
                "Folder",
                None,
                Some(&m),
                false,
                vec!["Press Enter to open this folder.".into()],
            ));
        }
        if !m.is_file() {
            return Ok(make(
                "Special file",
                None,
                Some(&m),
                false,
                vec!["Preview unavailable.".into()],
            ));
        }
        #[cfg(test)]
        BEFORE_PREVIEW_OPEN.with(|hook| {
            if let Some(hook) = hook.borrow_mut().take() {
                hook();
            }
        });
        let file = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)?;
        let live = file.metadata()?;
        if !live.is_file() || (m.dev(), m.ino()) != (live.dev(), live.ino()) {
            return Ok(make(
                "Unavailable",
                None,
                None,
                false,
                vec!["File changed while opening; refresh to try again.".into()],
            ));
        }
        const LIMIT: usize = 64 * 1024;
        let mut data = Vec::new();
        file.take((LIMIT + 1) as u64).read_to_end(&mut data)?;
        let mut short = data.len() > LIMIT;
        data.truncate(LIMIT);
        let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&data);
        let binary = || {
            make(
                "Binary file",
                Some(live.len()),
                Some(&live),
                false,
                vec!["Text preview unavailable.".into()],
            )
        };
        let text = match std::str::from_utf8(data) {
            Ok(s) => s,
            Err(e) if short && e.error_len().is_none() => {
                std::str::from_utf8(&data[..e.valid_up_to()]).map_err(io::Error::other)?
            }
            Err(_) => return Ok(binary()),
        };
        if text
            .chars()
            .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
        {
            return Ok(binary());
        }
        let normalized = text.replace("\r\n", "\n");
        let mut lines = Vec::new();
        for (i, line) in normalized
            .split_terminator(['\n', '\r', '\u{2028}', '\u{2029}'])
            .enumerate()
        {
            if i == 300 {
                short = true;
                break;
            }
            let mut buf = Vec::new();
            for c in line.chars() {
                if c == '\t' {
                    buf.resize(buf.len() + (4 - buf.len() % 4), ' ')
                } else {
                    buf.push(c)
                }
                if buf.len() > 512 {
                    buf.truncate(511);
                    buf.push('…');
                    short = true;
                    break;
                }
            }
            lines.push(format!(
                "{:>3} │ {}",
                i + 1,
                buf.into_iter().collect::<String>()
            ))
        }
        if lines.is_empty() {
            lines.push("Empty file.".into())
        }
        if short {
            lines.push("… Preview truncated (64 KiB / 300 lines / 512 characters per line).".into())
        }
        Ok(make(
            "Text file",
            Some(live.len()),
            Some(&live),
            short,
            lines,
        ))
    };
    read().unwrap_or_else(|e| {
        make(
            "Unavailable",
            None,
            None,
            false,
            vec![single_line(format!(
                "Cannot preview: {}",
                os_error(&e, path)
            ))],
        )
    })
}

#[cfg(test)]
mod tests {
    use super::{list, preview};
    use std::{fs, os::unix::fs::symlink, path::Path};

    #[test]
    fn listing_sorts_directories_before_files_and_filters_hidden() {
        let tmp = tempfile::tempdir().unwrap();
        fs::create_dir(tmp.path().join("z-folder")).unwrap();
        fs::write(tmp.path().join("a-file"), "a").unwrap();
        fs::write(tmp.path().join(".hidden"), "h").unwrap();
        let names = |hidden| {
            list(tmp.path(), hidden)
                .entries
                .into_iter()
                .map(|entry| {
                    entry
                        .path
                        .file_name()
                        .unwrap()
                        .to_string_lossy()
                        .into_owned()
                })
                .collect::<Vec<_>>()
        };
        assert_eq!(names(false), ["z-folder", "a-file"]);
        assert_eq!(names(true), ["z-folder", ".hidden", "a-file"]);
    }

    #[test]
    fn root_listing_has_only_root_as_neighbour() {
        let result = list(Path::new("/"), false);
        assert!(result.error.is_empty());
        assert_eq!(result.neighbours, [Path::new("/")]);
    }

    #[test]
    fn listing_formats_real_file_sizes_without_losing_units() {
        let tmp = tempfile::tempdir().unwrap();
        for (name, size) in [("a", 999_999), ("b", 1_000_000), ("c", 1_234_567)] {
            fs::File::create(tmp.path().join(name))
                .unwrap()
                .set_len(size)
                .unwrap();
        }
        let entries = list(tmp.path(), false).entries;
        let sizes: Vec<_> = entries.iter().map(|entry| entry.size.as_str()).collect();
        assert_eq!(sizes, ["999,999 B", "977 KiB", "1.2 MiB"]);
    }

    #[test]
    fn preview_shows_utf8_lines_and_byte_size() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("notes.txt");
        fs::write(&path, "first\n第二行\nlast\n").unwrap();
        let result = preview(&path);
        assert_eq!(result.lines, ["  1 │ first", "  2 │ 第二行", "  3 │ last"]);
        assert!(result.meta[1].contains("21 B"));
        assert!(!result.meta[1].contains("truncated"));
    }

    #[test]
    fn preview_rejects_binary_and_terminal_controls() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("binary");
        for bytes in [
            &b"text\0binary"[..],
            &b"\xff\xfe"[..],
            &b"hello\x1b[31m"[..],
        ] {
            fs::write(&path, bytes).unwrap();
            let result = preview(&path);
            assert!(result.meta[1].starts_with("Binary file"));
            assert!(!result.lines.iter().any(|line| line.contains('\x1b')));
        }
    }

    #[test]
    fn preview_limits_lines_and_long_lines() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("long");
        fs::write(&path, "x".repeat(513)).unwrap();
        let long = preview(&path);
        assert!(long.meta[1].contains("truncated"));
        assert_eq!(
            long.lines[0].split_once("│ ").unwrap().1.chars().count(),
            512
        );
        assert!(long.lines[0].ends_with('…'));
        fs::write(&path, "line\n".repeat(301)).unwrap();
        let many = preview(&path);
        assert!(many.meta[1].contains("truncated"));
        assert_eq!(many.lines.len(), 301);
        assert!(many.lines[299].starts_with("300 │ "));
    }

    #[test]
    fn preview_truncates_bytes_at_utf8_boundary() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("large");
        let mut bytes = format!("{}\n", "a".repeat(511)).repeat(127).into_bytes();
        bytes.extend_from_slice(&[b'a'; 511]);
        bytes.extend_from_slice("界more".as_bytes());
        fs::write(&path, bytes).unwrap();
        let result = preview(&path);
        assert!(result.meta[1].starts_with("Text file"));
        assert!(result.meta[1].contains("truncated"));
        assert_eq!(result.lines[127], format!("128 │ {}", "a".repeat(511)));
    }

    #[test]
    fn preview_symlink_and_missing_path_do_not_expose_contents() {
        let tmp = tempfile::tempdir().unwrap();
        let target = tmp.path().join("secret");
        fs::write(&target, "not shown").unwrap();
        let link = tmp.path().join("link");
        symlink(&target, &link).unwrap();
        let result = preview(&link);
        assert!(result.meta[1].starts_with("Link"));
        assert!(result.lines[0].contains(target.to_str().unwrap()));
        assert!(!result.lines.iter().any(|line| line.contains("not shown")));
        let missing = preview(&tmp.path().join("missing"));
        assert!(missing.meta[1].starts_with("Unavailable"));
        assert!(missing.lines[0].starts_with("Cannot preview:"));
    }

    #[test]
    fn preview_fifo_returns_without_blocking() {
        let tmp = tempfile::tempdir().unwrap();
        let fifo = tmp.path().join("pipe");
        rustix::fs::mkfifoat(rustix::fs::CWD, &fifo, rustix::fs::Mode::RUSR).unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || tx.send(preview(&fifo)).unwrap());
        let result = rx.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        assert!(result.meta[1].starts_with("Special file"));
    }

    #[test]
    fn preview_rejects_file_swapped_to_fifo_before_open() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("changing");
        fs::write(&path, "ordinary").unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let swapped = path.clone();
            super::BEFORE_PREVIEW_OPEN.with(|hook| {
                *hook.borrow_mut() = Some(Box::new(move || {
                    fs::remove_file(&swapped).unwrap();
                    rustix::fs::mkfifoat(rustix::fs::CWD, &swapped, rustix::fs::Mode::RUSR)
                        .unwrap();
                }));
            });
            tx.send(preview(&path)).unwrap();
        });
        let result = rx.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        assert!(result.meta[1].starts_with("Unavailable"));
        assert!(!result.lines.iter().any(|line| line.contains("ordinary")));
    }
}
