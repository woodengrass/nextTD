//! The `tdnext` management CLI: list, install, enable, disable, uninstall,
//! desktop, and run.
//!
//! Settings and the CLI share the `tdnext_manager` library; content programs
//! run as independent processes. Lifecycle commands resolve the shared
//! absolute state root through the manager and fail with a useful message
//! for a missing target or host.

mod config;
mod desktop;
mod icons;
mod launch;
mod local_env;
mod ops;
mod run;
mod session;

use std::process::ExitCode;

use anyhow::{Context, Result};
use tdnext_manager::{Action, Manager};

const USAGE: &str = "usage: tdnext <command> [arguments]

commands:
  list
  install <id>
  enable <id>
  disable <id>
  uninstall <id>
  desktop [DIR]
  run <id>";

fn usage_error(message: &str) -> ExitCode {
    eprintln!("tdnext: {message}\n{USAGE}");
    ExitCode::from(2)
}

fn dispatch(args: &[std::ffi::OsString]) -> Result<ExitCode> {
    let (command, rest) = args
        .split_first()
        .map(|(first, rest)| (first.to_str().unwrap_or("<non-UTF-8>"), rest))
        .unwrap_or(("", &[]));
    if command.is_empty() {
        return Ok(usage_error("no command given"));
    }
    if !matches!(
        command,
        "list" | "install" | "enable" | "disable" | "uninstall" | "desktop" | "run"
    ) {
        return Ok(usage_error(&format!("unknown command '{command}'")));
    }
    fn plugin_id(id: &std::ffi::OsStr) -> Result<&str> {
        id.to_str().context("plugin ID must be UTF-8")
    }
    match (command, rest) {
        ("list", []) => {
            ops::list(&Manager::load().context("loading plugin state")?)?;
        }
        ("install", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Install,
                plugin_id(id)?,
            )?;
        }
        ("enable", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Enable,
                plugin_id(id)?,
            )?;
        }
        ("disable", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Disable,
                plugin_id(id)?,
            )?;
        }
        ("uninstall", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Uninstall,
                plugin_id(id)?,
            )?;
        }
        ("desktop", []) => {
            desktop::desktop(
                &Manager::load().context("loading plugin state")?,
                std::path::Path::new("."),
            )?;
        }
        ("desktop", [directory]) => {
            desktop::desktop(
                &Manager::load().context("loading plugin state")?,
                std::path::Path::new(directory),
            )?;
        }
        ("run", [id]) => {
            run::run_view(
                &Manager::load().context("loading plugin state")?,
                plugin_id(id)?,
            )?;
        }
        _ => {
            return Ok(usage_error(&format!(
                "unexpected arguments for '{command}'"
            )));
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    match dispatch(&args) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("tdnext: {error:#}");
            ExitCode::FAILURE
        }
    }
}
