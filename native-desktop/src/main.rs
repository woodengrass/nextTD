mod config;
mod render;

use std::{
    env, fs,
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command, ExitCode},
};

use anyhow::{Context, Result, bail};
use clap::Parser;
use config::Config;

#[derive(Debug, Parser)]
#[command(about = "Standalone desktop for the patched tdnext Zellij core")]
struct Args {
    #[arg(long, hide = true)]
    internal_empty_pane: bool,
    /// Patched core executable (default: sibling named tdnext-core)
    #[arg(long)]
    core: Option<PathBuf>,
    /// JSON view configuration (default: sibling desktop.json, or an interactive shell)
    #[arg(long)]
    config: Option<PathBuf>,
    /// Initial directory for content programs
    #[arg(default_value = ".")]
    directory: PathBuf,
}

fn private_dir(path: &Path, uid: u32) -> Result<()> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Err(error) if error.kind() != io::ErrorKind::AlreadyExists => return Err(error.into()),
        _ => {}
    }
    validate_private_dir(path, uid, &path.symlink_metadata()?)
}

fn validate_private_dir(path: &Path, uid: u32, metadata: &fs::Metadata) -> Result<()> {
    if !metadata.is_dir() || metadata.uid() != uid || metadata.mode() & 0o077 != 0 {
        bail!(
            "{} must be a private directory owned by uid {uid}",
            path.display()
        );
    }
    Ok(())
}

fn socket_dir(directory: &Path) -> Result<PathBuf> {
    let uid = rustix::process::getuid().as_raw();
    if let Some(path) = env::var_os("ZELLIJ_SOCKET_DIR") {
        let contract = directory.join(&path).join("contract_version_1");
        match contract.symlink_metadata() {
            Ok(meta) => validate_private_dir(&contract, uid, &meta)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        return Ok(path.into());
    }
    let parent = if let Some(runtime) = env::var_os("XDG_RUNTIME_DIR") {
        let runtime = std::path::absolute(runtime)?;
        private_dir(&runtime, uid)?;
        runtime.join("tdnext")
    } else {
        PathBuf::from(format!("/tmp/tdnext-{uid}"))
    };
    private_dir(&parent, uid)?;
    let path = parent.join("zellij");
    private_dir(&path, uid)?;
    private_dir(&path.join("contract_version_1"), uid)?;
    Ok(path)
}

fn run(args: Args) -> Result<ExitCode> {
    if env::var_os("ZELLIJ").is_some() {
        bail!("cannot start a desktop inside an existing Zellij session");
    }
    let core = match args.core {
        Some(path) => {
            fs::canonicalize(&path).with_context(|| format!("opening core {}", path.display()))?
        }
        None => env::current_exe()?.with_file_name("tdnext-core"),
    };
    let probe = Command::new(&core)
        .arg("--tdnext-chrome-version")
        .output()
        .with_context(|| format!("starting core {}", core.display()))?;
    if !probe.status.success() || probe.stdout != b"1\n" {
        bail!(
            "{} is not a compatible tdnext chrome v1 core",
            core.display()
        );
    }
    let directory = fs::canonicalize(&args.directory)
        .with_context(|| format!("opening directory {}", args.directory.display()))?;
    if !directory.is_dir() {
        bail!("{} is not a directory", directory.display());
    }
    let config_path = match args.config {
        Some(path) => Some(path),
        None => {
            let path = env::current_exe()?.with_file_name("desktop.json");
            path.try_exists()?.then_some(path)
        }
    };
    let config = Config::load(config_path.as_deref())?;
    let temp = tempfile::Builder::new()
        .prefix("tdnext-desktop-")
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()?;
    fs::create_dir(temp.path().join("layouts"))?;
    fs::create_dir(temp.path().join("data"))?;
    let config_path = temp.path().join("config.kdl");
    let layout_path = temp.path().join("layout.kdl");
    let chrome_path = temp.path().join("chrome.json");
    fs::write(&config_path, render::settings(&config)?)?;
    fs::write(&layout_path, render::layout(&config)?)?;
    fs::write(&chrome_path, render::chrome(&config)?)?;
    let mut command = Command::new(&core);
    if let Some(state) = env::var_os("TDNEXT_STATE_DIR") {
        let path = PathBuf::from(state);
        let path = if let Ok(tail) = path.strip_prefix("~") {
            env::var_os("HOME")
                .map_or_else(|| PathBuf::from("/root"), PathBuf::from)
                .join(tail)
        } else {
            path
        };
        command.env("TDNEXT_STATE_DIR", env::current_dir()?.join(path));
    }
    let status = command
        .env("ZELLIJ_SOCKET_DIR", socket_dir(&directory)?)
        .args(["--config"])
        .arg(&config_path)
        .arg("--config-dir")
        .arg(temp.path())
        .arg("--data-dir")
        .arg(temp.path().join("data"))
        .arg("--new-session-with-layout")
        .arg(&layout_path)
        .env("TDNEXT_CHROME_FILE", &chrome_path)
        .env("TDNEXT_ZELLIJ", &core)
        .env("TDNEXT_DIRECTORY", &directory)
        .env("XDG_CACHE_HOME", temp.path().join("cache"))
        .current_dir(&directory)
        .status()
        .context("running desktop core")?;
    if !status.success() {
        bail!("desktop core exited with {status}");
    }
    Ok(ExitCode::SUCCESS)
}

fn main() -> ExitCode {
    let args = Args::parse();
    let result = if args.internal_empty_pane {
        (|| -> Result<ExitCode> {
            let mut stdout = io::stdout().lock();
            writeln!(
                stdout,
                "No views enabled. Enable a view and restart the desktop."
            )?;
            stdout.flush()?;
            io::copy(&mut io::stdin().lock(), &mut io::sink())?;
            Ok(ExitCode::SUCCESS)
        })()
    } else {
        run(args)
    };
    match result {
        Ok(code) => code,
        Err(error) => {
            eprintln!("tdnext-desktop: {error:#}");
            ExitCode::FAILURE
        }
    }
}
