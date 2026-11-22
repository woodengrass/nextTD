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

pub fn single_line(s: &str) -> String {
    s.chars()
        .map(|c| match get_general_category(c) {
            G::Control
            | G::Format
            | G::Surrogate
            | G::PrivateUse
            | G::Unassigned
            | G::LineSeparator
            | G::ParagraphSeparator => '?',
            G::SpaceSeparator if c != ' ' => '?',
            _ => c,
        })
        .collect()
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
pub fn preview(path: &Path) -> Preview {
    let name = single_line(&path.file_name().unwrap_or_default().to_string_lossy());
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
                vec![single_line(&format!(
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
            vec![single_line(&format!(
                "Cannot preview: {}",
                os_error(&e, path)
            ))],
        )
    })
}
