//! Pinned, checksum-verified, user-local executable installation.
//!
//! Port of `src/tdnext/core/executables.py` (`install_release`) and the monitor plugin's
//! `src/tdnext/plugins/monitor/dependencies.py` (`BTOP_RELEASE`,
//! `MonitorDependencies::check` / `::install`).
//!
//! The caller owns consent: [`description`] renders the exact approval text
//! the manager/CLI shows the user, and [`install`] runs only after approval.
//! Nothing here installs anything on import or at startup.

use std::ffi::{OsStr, OsString};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::Duration;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt as _;

use wait_timeout::ChildExt;

const BTOP_NAME: &str = "btop";
const BTOP_VERSION: &str = "1.4.8";
const BTOP_URL: &str = "https://github.com/aristocratos/btop/releases/download/v1.4.8/btop-x86_64-unknown-linux-musl.tar.gz";
const BTOP_SHA256: &str = "af589cf82cc9ea9c29ce050b9239a13bef2d1492256d476156e1054a42ce89ad";
const BTOP_MEMBER: &str = "./btop/bin/btop";

/// One allowlisted downloadable release.
struct Release {
    name: &'static str,
    version: &'static str,
    url: &'static str,
    sha256: &'static str,
    member: &'static str,
}

const RELEASES: &[Release] = &[
    Release {
        name: BTOP_NAME,
        version: BTOP_VERSION,
        url: BTOP_URL,
        sha256: BTOP_SHA256,
        member: BTOP_MEMBER,
    },
    #[cfg(test)]
    Release {
        name: "test-tool",
        version: "1",
        url: "test.invalid",
        sha256: "unused",
        member: "test-tool",
    },
];

fn release_for(name: &str) -> Option<&'static Release> {
    RELEASES.iter().find(|release| release.name == name)
}

/// Exact consent text for the approval flow: URL, digest, archive path,
/// and install destination — the string the user approves.
pub fn description(name: &str, bin: &Path) -> anyhow::Result<String> {
    let release = release_for(name).ok_or_else(|| {
        anyhow::anyhow!("No local installer for '{name}'; install it yourself and add it to PATH.")
    })?;
    Ok(format!(
        "Download {} {} from {}; SHA-256 {}; extract {}; install to {}",
        release.name,
        release.version,
        release.url,
        release.sha256,
        release.member,
        bin.join(release.name).display(),
    ))
}

/// Retain the verified inode until the state commit. Never overwrite a user file
/// on publication, or remove a replacement if the operation rolls back.
pub(crate) struct PendingTool {
    destination: PathBuf,
    candidate: PathBuf,
    _work: tempfile::TempDir,
    committed: bool,
}

impl PendingTool {
    pub(crate) fn commit(mut self) {
        self.committed = true;
    }
}

impl Drop for PendingTool {
    fn drop(&mut self) {
        use std::os::unix::fs::MetadataExt;
        if !self.committed
            && let (Ok(original), Ok(current)) = (
                self.candidate.metadata(),
                self.destination.symlink_metadata(),
            )
            && original.dev() == current.dev()
            && original.ino() == current.ino()
            && let Err(error) = fs::remove_file(&self.destination)
        {
            eprintln!("Cannot roll back {}: {error}", self.destination.display());
        }
    }
}

/// Download one allowlisted binary, never extracting arbitrary archive paths.
pub(crate) fn install(name: &str, bin: &Path) -> anyhow::Result<PendingTool> {
    let release = release_for(name).ok_or_else(|| {
        anyhow::anyhow!("No local installer for '{name}'; install it yourself and add it to PATH.")
    })?;
    install_release(release, bin)
}

/// A working btop must report its name via `--version`.
pub fn check_btop(binary: &Path) -> anyhow::Result<()> {
    let output = run_with_timeout(binary, &["--version"], Duration::from_secs(10))
        .map_err(|exc| anyhow::anyhow!("Cannot run btop at {}: {exc}", binary.display()))?;
    let Some(output) = output else {
        return Err(anyhow::anyhow!(
            "Cannot run btop at {}: timed out after 10s",
            binary.display()
        ));
    };
    if !output.status.success() || !lossy(&output.stdout).to_lowercase().contains("btop") {
        return Err(anyhow::anyhow!(
            "Executable at {} is not a working btop",
            binary.display()
        ));
    }
    Ok(())
}

fn install_release(release: &Release, bin: &Path) -> anyhow::Result<PendingTool> {
    let name = release.name;
    if std::env::consts::OS != "linux" || std::env::consts::ARCH != "x86_64" {
        anyhow::bail!("No bundled {name} release for this platform; install it on PATH.");
    }
    let curl = which("curl").ok_or_else(|| {
        anyhow::anyhow!("Cannot install {name}: curl is required for the verified download.")
    })?;

    fs::create_dir_all(bin).map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let work = tempfile::Builder::new()
        .prefix(&format!(".{name}-"))
        .tempdir_in(bin)
        .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let archive = work.path().join("release.tar.gz");

    let output = run_with_timeout(
        &curl,
        &[
            OsString::from("--fail"),
            OsString::from("--location"),
            OsString::from("--silent"),
            OsString::from("--show-error"),
            OsString::from("--connect-timeout"),
            OsString::from("10"),
            OsString::from("--max-time"),
            OsString::from("120"),
            OsString::from("--output"),
            archive.clone().into_os_string(),
            OsString::from(release.url),
        ],
        Duration::from_secs(130),
    )
    .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let Some(output) = output else {
        return Err(anyhow::anyhow!("Download of {name} failed: timed out"));
    };
    if !output.status.success() {
        return Err(anyhow::anyhow!(
            "Download of {name} failed: {}",
            lossy(&output.stderr).trim()
        ));
    }

    let digest = sha256_of(&archive, name)?;
    if digest != release.sha256 {
        return Err(anyhow::anyhow!(
            "Checksum mismatch for {name}: expected {}, got {digest}",
            release.sha256
        ));
    }

    assert_regular_member(&archive, release)?;
    let candidate = work.path().join(name);
    extract_member_to(&archive, release.member, &candidate, name)?;

    #[cfg(unix)]
    fs::set_permissions(&candidate, fs::Permissions::from_mode(0o755))
        .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;

    let output = run_with_timeout(&candidate, &["--version"], Duration::from_secs(10))
        .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let Some(output) = output else {
        return Err(anyhow::anyhow!("Installed {name} could not run: timed out"));
    };
    if !output.status.success() {
        return Err(anyhow::anyhow!(
            "Installed {name} could not run: {}",
            lossy(&output.stderr).trim()
        ));
    }

    if name == BTOP_NAME && !lossy(&output.stdout).to_lowercase().contains("btop") {
        anyhow::bail!("Installed executable is not a working btop");
    }
    let destination = bin.join(name);
    fs::hard_link(&candidate, &destination)
        .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    Ok(PendingTool {
        destination,
        candidate,
        _work: work,
        committed: false,
    })
}

/// SHA-256 of a file via the system `sha256sum` (no extra crypto dependency).
fn sha256_of(archive: &Path, name: &str) -> anyhow::Result<String> {
    let sha256sum = which("sha256sum").ok_or_else(|| {
        anyhow::anyhow!(
            "Failed to install {name}: sha256sum is required for checksum verification."
        )
    })?;
    let output = run_with_timeout(&sha256sum, &[archive.as_os_str()], Duration::from_secs(60))
        .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let Some(output) = output else {
        return Err(anyhow::anyhow!(
            "Failed to install {name}: checksum timed out"
        ));
    };
    if !output.status.success() {
        return Err(anyhow::anyhow!(
            "Failed to install {name}: {}",
            lossy(&output.stderr).trim()
        ));
    }
    lossy(&output.stdout)
        .split_whitespace()
        .next()
        .filter(|digest| digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_hexdigit()))
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!("Failed to install {name}: cannot parse sha256sum output."))
}

/// The archived executable must be a regular file; anything else is rejected
/// before anything is written to disk.
fn assert_regular_member(archive: &Path, release: &Release) -> anyhow::Result<()> {
    let name = release.name;
    let output = run_with_timeout(
        Path::new("tar"),
        &[
            OsString::from("--list"),
            OsString::from("--verbose"),
            OsString::from("--gzip"),
            format!("--file={}", archive.display()).into(),
            OsString::from("--"),
            OsString::from(release.member),
        ],
        Duration::from_secs(60),
    )
    .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let Some(output) = output else {
        return Err(anyhow::anyhow!("Invalid {name} archive: listing timed out"));
    };
    if !output.status.success() {
        return Err(anyhow::anyhow!(
            "Invalid {name} archive: executable is not a regular file"
        ));
    }
    let regular = lossy(&output.stdout).lines().any(|line| {
        let mut fields = line.split_whitespace();
        matches!(
            fields.next().map(str::as_bytes).map(|b| b.first()),
            Some(Some(b'-'))
        ) && fields.last().is_some_and(|last| last == release.member)
    });
    if !regular {
        return Err(anyhow::anyhow!(
            "Invalid {name} archive: executable is not a regular file"
        ));
    }
    Ok(())
}

/// Stream the single allowlisted member to `candidate`; no archive path ever
/// touches the filesystem, so sibling entries cannot escape the work dir.
fn extract_member_to(
    archive: &Path,
    member: &str,
    candidate: &Path,
    name: &str,
) -> anyhow::Result<()> {
    let output = run_with_timeout(
        Path::new("tar"),
        &[
            OsString::from("--extract"),
            OsString::from("--gzip"),
            format!("--file={}", archive.display()).into(),
            OsString::from("--to-stdout"),
            OsString::from("--"),
            OsString::from(member),
        ],
        Duration::from_secs(120),
    )
    .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    let Some(output) = output else {
        return Err(anyhow::anyhow!(
            "Invalid {name} archive: extraction timed out"
        ));
    };
    if !output.status.success() || output.stdout.is_empty() {
        return Err(anyhow::anyhow!(
            "Invalid {name} archive: executable is unreadable"
        ));
    }
    fs::write(candidate, &output.stdout)
        .map_err(|exc| anyhow::anyhow!("Failed to install {name}: {exc}"))?;
    Ok(())
}

/// Run `program` with piped I/O and a real finite timeout. Returns `Ok(None)`
/// on timeout (the child is killed and reaped); stdout/stderr are drained
/// concurrently so a large `--to-stdout` payload cannot deadlock the wait.
fn run_with_timeout(
    program: &Path,
    args: &[impl AsRef<OsStr>],
    timeout: Duration,
) -> std::io::Result<Option<Output>> {
    use rustix::process::{Pid, Signal, kill_process_group};
    use std::os::unix::process::CommandExt as _;
    use std::sync::mpsc;
    let deadline = std::time::Instant::now() + timeout;
    let remaining = || deadline.saturating_duration_since(std::time::Instant::now());
    let mut child = Command::new(program)
        .args(args)
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    fn drain(mut pipe: impl Read + Send + 'static) -> mpsc::Receiver<std::io::Result<Vec<u8>>> {
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = pipe.read_to_end(&mut bytes).map(|_| bytes);
            let _ = tx.send(result);
        });
        rx
    }
    let stdout = drain(
        child
            .stdout
            .take()
            .ok_or_else(|| std::io::Error::other("missing stdout pipe"))?,
    );
    let stderr = drain(
        child
            .stderr
            .take()
            .ok_or_else(|| std::io::Error::other("missing stderr pipe"))?,
    );
    if let Some(status) = child.wait_timeout(remaining())?
        && let Ok(stdout) = stdout.recv_timeout(remaining())
        && let Ok(stderr) = stderr.recv_timeout(remaining())
    {
        return Ok(Some(Output {
            status,
            stdout: stdout?,
            stderr: stderr?,
        }));
    }
    let _ = kill_process_group(Pid::from_child(&child), Signal::KILL);
    let _ = child.kill();
    child.wait()?;
    Ok(None)
}

/// `shutil.which` equivalent: first executable `name` on `PATH`.
fn which(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    std::env::split_paths(&path_var)
        .map(|dir| dir.join(name))
        .find(|candidate| is_executable(candidate))
}

#[cfg(unix)]
fn is_executable(candidate: &Path) -> bool {
    candidate.is_file()
        && candidate
            .metadata()
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(candidate: &Path) -> bool {
    candidate.is_file()
}

fn lossy(bytes: &[u8]) -> std::borrow::Cow<'_, str> {
    String::from_utf8_lossy(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use anyhow::{Context, Result};

    fn expect_err<T>(result: Result<T>) -> Result<anyhow::Error> {
        match result {
            Ok(_) => anyhow::bail!("expected an error"),
            Err(error) => Ok(error),
        }
    }

    #[test]
    fn consent_text_names_url_digest_path_and_destination() -> Result<()> {
        let text = description("btop", Path::new("/state/bin"))?;
        assert_eq!(
            text,
            "Download btop 1.4.8 from https://github.com/aristocratos/btop/releases/download/v1.4.8/btop-x86_64-unknown-linux-musl.tar.gz; \
             SHA-256 af589cf82cc9ea9c29ce050b9239a13bef2d1492256d476156e1054a42ce89ad; \
             extract ./btop/bin/btop; install to /state/bin/btop"
        );
        Ok(())
    }

    #[test]
    fn unknown_name_has_no_installer() -> Result<()> {
        let error = expect_err(description("htop", Path::new("/state/bin")))?;
        assert_eq!(
            error.to_string(),
            "No local installer for 'htop'; install it yourself and add it to PATH."
        );
        Ok(())
    }

    /// Write-then-rename so the executed path was never opened for writing;
    /// a setup-only retry then absorbs a transient busy-file hold from the
    /// environment's file handling. The asserted behavior below stays a
    /// single deterministic check.
    fn write_probe(dir: &Path, name: &str, body: &str) -> Result<PathBuf> {
        let staging = dir.join(format!(".{name}.staging"));
        let path = dir.join(name);
        fs::write(&staging, body).with_context(|| format!("write {}", staging.display()))?;
        fs::rename(&staging, &path).with_context(|| format!("publish {}", path.display()))?;
        #[cfg(unix)]
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755))?;
        settle_exec(&path)?;
        Ok(path)
    }

    fn settle_exec(program: &Path) -> Result<()> {
        for _ in 0..1000 {
            match std::process::Command::new(program)
                .arg("--version")
                .output()
            {
                Err(error) if error.raw_os_error() == Some(26) => {
                    std::thread::yield_now();
                }
                _ => return Ok(()),
            }
        }
        anyhow::bail!("test setup could not execute {}", program.display())
    }

    /// The rejection probe is a symlink to an immutable system program:
    /// nothing is written, so a busy-file hold is impossible by construction.
    #[cfg(unix)]
    fn rejection_probe(dir: &Path) -> Result<PathBuf> {
        let target = which("true").context("test setup needs `true` on PATH")?;
        let path = dir.join("not-btop");
        std::os::unix::fs::symlink(&target, &path)?;
        Ok(path)
    }

    #[cfg(not(unix))]
    fn rejection_probe(dir: &Path) -> Result<PathBuf> {
        write_probe(dir, "not-btop", "#!/bin/sh\necho 'htop 3.0'\n")
    }

    #[test]
    fn working_btop_must_report_its_name() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let good = write_probe(dir.path(), "fake-btop", "#!/bin/sh\necho 'btop 1.4.8'\n")?;
        check_btop(&good)?;

        let impostor = rejection_probe(dir.path())?;
        let error = expect_err(check_btop(&impostor))?;
        assert!(
            error.to_string().contains("is not a working btop"),
            "{error:?}"
        );
        Ok(())
    }
}
