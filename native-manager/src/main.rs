//! The `tdnext` management CLI: list, install, enable, disable, uninstall,
//! install-zellij, desktop, and run.
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
  install-zellij
  desktop [DIR]
  run <id>";

fn usage_error(message: &str) -> ExitCode {
    eprintln!("tdnext: {message}\n{USAGE}");
    ExitCode::from(2)
}

fn dispatch(args: &[String]) -> Result<ExitCode> {
    let (command, rest) = args
        .split_first()
        .map(|(first, rest)| (first.as_str(), rest))
        .unwrap_or(("", &[]));
    if command.is_empty() {
        return Ok(usage_error("no command given"));
    }
    if !matches!(
        command,
        "list"
            | "install"
            | "enable"
            | "disable"
            | "uninstall"
            | "install-zellij"
            | "desktop"
            | "run"
    ) {
        return Ok(usage_error(&format!("unknown command '{command}'")));
    }
    match (command, rest) {
        ("list", []) => {
            ops::list(&Manager::load().context("loading plugin state")?)?;
        }
        ("install", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Install,
                id,
            )?;
        }
        ("enable", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Enable,
                id,
            )?;
        }
        ("disable", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Disable,
                id,
            )?;
        }
        ("uninstall", [id]) => {
            ops::apply_and_report(
                &Manager::load().context("loading plugin state")?,
                Action::Uninstall,
                id,
            )?;
        }
        ("install-zellij", []) => {
            ops::install_zellij(&Manager::load().context("loading plugin state")?)?;
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
            run::run_view(&Manager::load().context("loading plugin state")?, id)?;
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
    let args: Vec<String> = std::env::args().skip(1).collect();
    match dispatch(&args) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("tdnext: {error:#}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod dispatch_tests {
    #[test]
    fn usage_names_every_subcommand() {
        for command in [
            "list",
            "install",
            "enable",
            "disable",
            "uninstall",
            "install-zellij",
            "desktop",
            "run",
        ] {
            assert!(
                super::USAGE.contains(command),
                "usage must document {command}"
            );
        }
    }
}
