//! The `run <id>` subcommand: execute one enabled view's program.
//!
//! Every desktop pane starts here, so per-view environment isolation lives in
//! exactly one place: the interactive shell runs with the user's original
//! environment, the monitor runs with an isolated XDG environment, and every
//! other view inherits the pane environment. Commands are executed directly,
//! never through a shell, so configured arguments keep their exact meaning.

use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result};
use tdnext_manager::{Manager, Manifest};

use crate::local_env::{restore_vars, snapshot};

/// Owned view data; the one-way adapter is [`ViewSpec::of`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ViewSpec {
    pub id: String,
    pub name: String,
    pub placement: String,
    pub command: Option<String>,
    pub args: Vec<String>,
    pub icon: Option<String>,
    pub icon_key: Option<String>,
    pub role: String,
    pub view_interfaces: Vec<String>,
}

impl ViewSpec {
    #[must_use]
    pub fn of(manifest: &Manifest) -> Self {
        Self {
            id: manifest.id.clone(),
            name: manifest.name.clone(),
            placement: manifest.placement.clone(),
            command: manifest.command.clone(),
            args: manifest.args.clone(),
            icon: manifest.icon.clone(),
            icon_key: manifest.icon_key.clone(),
            role: manifest.role().to_string(),
            view_interfaces: manifest.view_interfaces.clone(),
        }
    }
}

/// Resolved program plus its exact argument list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    pub program: PathBuf,
    pub args: Vec<String>,
}

#[cfg(unix)]
fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt as _;
    path.is_file()
        && std::fs::metadata(path).is_ok_and(|metadata| metadata.permissions().mode() & 0o111 != 0)
}

#[cfg(not(unix))]
fn is_executable_file(path: &Path) -> bool {
    path.is_file()
}

fn missing_message(id: &str, name: &str) -> String {
    if id == "monitor" {
        "btop is missing. Run: tdnext enable monitor".to_string()
    } else {
        format!("{name} is missing; install it in state/bin or on PATH")
    }
}

/// The user's interactive shell: `$SHELL` wins, the view's configured
/// command is the fallback, `/bin/sh` the last resort.
#[must_use]
pub fn user_shell(configured: Option<&str>, env_shell: Option<&OsStr>) -> PathBuf {
    if let Some(shell) = env_shell.filter(|shell| !shell.is_empty()) {
        return PathBuf::from(shell);
    }
    configured.map_or_else(|| PathBuf::from("/bin/sh"), PathBuf::from)
}

/// Resolve one named program without shell interpolation: a path containing
/// a separator is checked directly, a bare name goes through the manager
/// lookup (state bin, sibling binary, then PATH).
fn resolve_named(
    id: &str,
    command: &str,
    missing: &dyn Fn() -> String,
    lookup: &dyn Fn(&str) -> Option<PathBuf>,
) -> Result<PathBuf> {
    if command.contains('/') {
        let program = PathBuf::from(command);
        if !is_executable_file(&program) {
            anyhow::bail!(
                "configured command {} for view '{id}' is missing or not executable",
                program.display()
            );
        }
        return Ok(program);
    }
    lookup(command).with_context(missing)
}

/// Resolve the view program. The shell runs the user's `$SHELL`; every other
/// view runs its configured command.
pub fn resolve(
    spec: &ViewSpec,
    lookup: &dyn Fn(&str) -> Option<PathBuf>,
    shell_env: Option<&OsStr>,
) -> Result<Resolved> {
    let mut args = spec.args.clone();
    if spec.id == "shell" && !args.iter().any(|arg| arg == "-i") {
        args.push("-i".to_string());
    }
    if spec.id == "shell" {
        let program = user_shell(spec.command.as_deref(), shell_env);
        let Some(name) = program.to_str() else {
            anyhow::bail!("user shell {} is not valid UTF-8", program.display());
        };
        let program = resolve_named(
            &spec.id,
            name,
            &|| format!("user shell {name} is missing or not executable"),
            lookup,
        )?;
        return Ok(Resolved { program, args });
    }
    let command = spec
        .command
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("view '{}' declares no executable command", spec.id))?;
    let program = resolve_named(
        &spec.id,
        command,
        &|| missing_message(&spec.id, command),
        lookup,
    )?;
    Ok(Resolved { program, args })
}

/// Everything `run` needs beyond program and arguments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Spawn {
    pub workdir: Option<PathBuf>,
    pub extra_env: Vec<(OsString, OsString)>,
    pub restore_env: bool,
}

/// Per-view environment: the shell starts in the desktop directory with the
/// user's original environment; the monitor gets an isolated XDG
/// environment; other views inherit the pane environment unchanged.
pub fn spawn_for(root: &Path, spec: &ViewSpec, directory: Option<&OsStr>) -> Result<Spawn> {
    if spec.id == "shell" {
        let directory = directory.ok_or_else(|| {
            anyhow::anyhow!("TDNEXT_DIRECTORY is not set; launch through `tdnext desktop`")
        })?;
        return Ok(Spawn {
            workdir: Some(PathBuf::from(directory)),
            extra_env: Vec::new(),
            restore_env: true,
        });
    }
    if spec.id == "monitor" {
        let btop = root.join("btop");
        return Ok(Spawn {
            workdir: None,
            extra_env: vec![
                (
                    OsString::from("XDG_CONFIG_HOME"),
                    btop.join("config").into_os_string(),
                ),
                (
                    OsString::from("XDG_DATA_HOME"),
                    btop.join("data").into_os_string(),
                ),
                (
                    OsString::from("XDG_CACHE_HOME"),
                    btop.join("cache").into_os_string(),
                ),
            ],
            restore_env: false,
        });
    }
    Ok(Spawn {
        workdir: None,
        extra_env: Vec::new(),
        restore_env: false,
    })
}

fn enabled_view_ids(
    manifests: &std::collections::BTreeMap<String, Manifest>,
    enabled: &BTreeSet<String>,
) -> Vec<String> {
    manifests
        .values()
        .filter(|manifest| manifest.role() == "desktop.view" && enabled.contains(&manifest.id))
        .map(|manifest| manifest.id.clone())
        .collect()
}

pub fn run_view(manager: &Manager, id: &str) -> Result<()> {
    let state = manager.state()?;
    let manifest = manager.manifests.get(id);
    let Some(manifest) = manifest else {
        if state.installed.contains(id) {
            anyhow::bail!("plugin '{id}' is installed but has no usable manifest");
        }
        let enabled = enabled_view_ids(&manager.manifests, &state.enabled);
        let hint = if enabled.is_empty() {
            "(none)".to_string()
        } else {
            enabled.join(", ")
        };
        anyhow::bail!("unknown view '{id}'; enabled views: {hint}");
    };
    if manifest.role() != "desktop.view" {
        anyhow::bail!("'{}' is not a runnable view (role {})", id, manifest.role());
    }
    if !state.enabled.contains(id) {
        anyhow::bail!("view '{id}' is not enabled; run `tdnext enable {id}` first");
    }
    let spec = ViewSpec::of(manifest);
    let shell_env = std::env::var_os("SHELL");
    let resolved = resolve(
        &spec,
        &|name| manager.executable(name),
        shell_env.as_deref(),
    )?;
    let directory = std::env::var_os("TDNEXT_DIRECTORY");
    let spawn = spawn_for(&manager.root, &spec, directory.as_deref())?;
    let mut command = Command::new(&resolved.program);
    command.args(&resolved.args);
    if let Some(workdir) = &spawn.workdir {
        command.current_dir(workdir);
    }
    if spawn.restore_env {
        let mut vars = snapshot();
        restore_vars(&mut vars);
        command.env_clear().envs(&vars);
    }
    command.envs(spawn.extra_env.clone());
    let error = std::os::unix::process::CommandExt::exec(&mut command);
    Err(anyhow::anyhow!(
        "cannot start {}: {error}",
        resolved.program.display()
    ))
}

#[cfg(test)]
mod tests {
    use super::{ViewSpec, missing_message, resolve, spawn_for};
    use anyhow::{Context, Result};
    use std::ffi::OsString;
    use std::path::{Path, PathBuf};

    fn spec(id: &str, command: Option<&str>, args: &[&str]) -> ViewSpec {
        ViewSpec {
            id: id.to_string(),
            name: id.to_string(),
            placement: "sidebar".to_string(),
            command: command.map(str::to_string),
            args: args.iter().map(ToString::to_string).collect(),
            icon: None,
            icon_key: None,
            role: "desktop.view".to_string(),
            view_interfaces: Vec::new(),
        }
    }

    fn missing(_: &str) -> Option<PathBuf> {
        None
    }

    fn expect_err<T>(result: Result<T>) -> Result<anyhow::Error> {
        match result {
            Ok(_) => anyhow::bail!("expected an error"),
            Err(error) => Ok(error),
        }
    }

    #[test]
    fn shell_gains_exactly_one_interactive_flag() -> Result<()> {
        let resolved = resolve(&spec("shell", Some("/bin/sh"), &[]), &missing, None)?;
        assert_eq!(resolved.args, vec!["-i".to_string()]);
        let resolved = resolve(&spec("shell", Some("/bin/sh"), &["-i"]), &missing, None)?;
        assert_eq!(resolved.args, vec!["-i".to_string()]);
        Ok(())
    }

    #[test]
    fn custom_shell_command_keeps_its_arguments() -> Result<()> {
        let resolved = resolve(
            &spec("shell", Some("/bin/bash"), &["--noprofile"]),
            &missing,
            None,
        )?;
        assert_eq!(resolved.program, PathBuf::from("/bin/bash"));
        assert_eq!(
            resolved.args,
            vec!["--noprofile".to_string(), "-i".to_string()]
        );
        Ok(())
    }

    #[test]
    fn shell_prefers_the_users_shell_over_the_manifest() -> Result<()> {
        let resolved = resolve(
            &spec("shell", Some("/nonexistent-manifest-shell"), &[]),
            &missing,
            Some(OsString::from("/bin/sh").as_os_str()),
        )?;
        assert_eq!(resolved.program, PathBuf::from("/bin/sh"));
        assert_eq!(resolved.args, vec!["-i".to_string()]);
        Ok(())
    }

    #[test]
    fn shell_falls_back_to_the_manifest_without_shell_env() -> Result<()> {
        let resolved = resolve(
            &spec("shell", Some("/bin/sh"), &[]),
            &missing,
            Some(OsString::from("").as_os_str()),
        )?;
        assert_eq!(resolved.program, PathBuf::from("/bin/sh"));
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn shell_rejects_non_utf8_shell_env() -> Result<()> {
        use std::os::unix::ffi::OsStringExt as _;
        let shell = OsString::from_vec(vec![0xff]);
        let error = expect_err(resolve(
            &spec("shell", Some("/bin/sh"), &[]),
            &missing,
            Some(shell.as_os_str()),
        ))?;
        assert!(error.to_string().contains("not valid UTF-8"), "{error:?}");
        Ok(())
    }

    #[test]
    fn missing_btop_keeps_the_legacy_message() -> Result<()> {
        let error = expect_err(resolve(&spec("monitor", Some("btop"), &[]), &missing, None))?;
        assert_eq!(error.to_string(), missing_message("monitor", "btop"));
        assert_eq!(
            error.to_string(),
            "btop is missing. Run: tdnext enable monitor"
        );
        Ok(())
    }

    #[test]
    fn missing_files_names_state_bin_and_path() -> Result<()> {
        let error = expect_err(resolve(
            &spec("files", Some("tdnext-files"), &[]),
            &missing,
            None,
        ))?;
        assert_eq!(
            error.to_string(),
            "tdnext-files is missing; install it in state/bin or on PATH"
        );
        Ok(())
    }

    #[test]
    fn configured_absolute_path_must_exist() -> Result<()> {
        let error = expect_err(resolve(
            &spec("app", Some("/nonexistent/tdnext-app"), &[]),
            &missing,
            None,
        ))?;
        assert!(
            error.to_string().contains("missing or not executable"),
            "{error:?}"
        );
        assert!(error.to_string().contains("app"), "{error:?}");
        Ok(())
    }

    #[test]
    fn configured_arguments_are_never_split() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let program = dir.path().join("probe");
        std::fs::write(&program, "#!/bin/sh\n")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            let mut permissions = std::fs::metadata(&program)?.permissions();
            permissions.set_mode(0o755);
            std::fs::set_permissions(&program, permissions)?;
        }
        let program_text = program
            .to_str()
            .with_context(|| "probe path is not UTF-8")?;
        let resolved = resolve(
            &spec("app", Some(program_text), &["a b; rm -rf /", "c"]),
            &missing,
            None,
        )?;
        assert_eq!(
            resolved.args,
            vec!["a b; rm -rf /".to_string(), "c".to_string()]
        );
        Ok(())
    }

    #[test]
    fn lookup_resolves_bare_names() -> Result<()> {
        let resolved = resolve(
            &spec("files", Some("tdnext-files"), &[]),
            &|name| (name == "tdnext-files").then(|| PathBuf::from("/state/bin/tdnext-files")),
            None,
        )?;
        assert_eq!(resolved.program, PathBuf::from("/state/bin/tdnext-files"));
        assert!(resolved.args.is_empty());
        Ok(())
    }

    #[test]
    fn view_without_command_is_rejected() -> Result<()> {
        let error = expect_err(resolve(&spec("app", None, &[]), &missing, None))?;
        assert!(
            error.to_string().contains("no executable command"),
            "{error:?}"
        );
        Ok(())
    }

    #[test]
    fn shell_starts_in_the_desktop_directory_with_restored_env() -> Result<()> {
        let spawn = spawn_for(
            Path::new("/state"),
            &spec("shell", Some("/bin/sh"), &[]),
            Some(OsString::from("/work").as_os_str()),
        )?;
        assert_eq!(spawn.workdir, Some(PathBuf::from("/work")));
        assert!(spawn.restore_env);
        assert!(spawn.extra_env.is_empty());
        let error = expect_err(spawn_for(
            Path::new("/state"),
            &spec("shell", Some("/bin/sh"), &[]),
            None,
        ))?;
        assert!(error.to_string().contains("TDNEXT_DIRECTORY"), "{error:?}");
        Ok(())
    }

    #[test]
    fn monitor_gets_an_isolated_xdg_environment() -> Result<()> {
        let spawn = spawn_for(
            Path::new("/state"),
            &spec("monitor", Some("btop"), &[]),
            None,
        )?;
        assert!(spawn.workdir.is_none());
        assert!(!spawn.restore_env);
        let collected: std::collections::BTreeMap<_, _> = spawn.extra_env.into_iter().collect();
        assert_eq!(
            collected
                .get(&OsString::from("XDG_CONFIG_HOME"))
                .map(|value| value.to_string_lossy().into_owned()),
            Some("/state/btop/config".to_string())
        );
        assert_eq!(
            collected
                .get(&OsString::from("XDG_DATA_HOME"))
                .map(|value| value.to_string_lossy().into_owned()),
            Some("/state/btop/data".to_string())
        );
        assert_eq!(
            collected
                .get(&OsString::from("XDG_CACHE_HOME"))
                .map(|value| value.to_string_lossy().into_owned()),
            Some("/state/btop/cache".to_string())
        );
        Ok(())
    }
}
