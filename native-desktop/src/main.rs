mod config;
mod render;

use std::{
    env, fs,
    io::{self, Write},
    path::PathBuf,
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
        .tempdir()?;
    fs::create_dir(temp.path().join("layouts"))?;
    fs::create_dir(temp.path().join("data"))?;
    let config_path = temp.path().join("config.kdl");
    let layout_path = temp.path().join("layout.kdl");
    let chrome_path = temp.path().join("chrome.json");
    fs::write(&config_path, render::settings(&config)?)?;
    fs::write(&layout_path, render::layout(&config)?)?;
    fs::write(&chrome_path, render::chrome(&config)?)?;
    let status = Command::new(&core)
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

#[test]
fn __ci_probe_test_failure() {
    let expected = 1;
    let actual = 2;
    assert_eq!(expected, actual, "ci probe: intentional failure");
}
