//! Desktop spawning: child environment and sibling process launch.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result};

use crate::local_env::{STATE_DIR_ENV, snapshot};

/// Everything the launcher child needs, resolved before spawning.
pub struct Launch {
    pub desktop_bin: PathBuf,
    pub core_bin: PathBuf,
    pub config_path: PathBuf,
    pub directory: PathBuf,
    pub host_id: String,
    pub protected: Vec<String>,
    pub root: PathBuf,
    pub extra: Vec<(OsString, OsString)>,
}

/// Assemble the child environment: the absolute shared state root plus the
/// session identity. The child's PATH passes through untouched, so the
/// Zellij server and every pane inherit the user's own PATH. Pure over the
/// caller's map so tests never touch process state.
pub fn child_env(
    base: &BTreeMap<OsString, OsString>,
    root: &Path,
    host_id: &str,
    protected: &[String],
) -> Result<BTreeMap<OsString, OsString>> {
    if base.contains_key(OsStr::new("ZELLIJ")) {
        anyhow::bail!("cannot start a desktop inside an existing Zellij session");
    }
    let mut vars = base.clone();
    vars.insert(
        OsString::from(STATE_DIR_ENV),
        root.as_os_str().to_os_string(),
    );
    vars.insert(OsString::from("TDNEXT_HOST_ID"), OsString::from(host_id));
    vars.insert(
        OsString::from("TDNEXT_PROTECTED_PLUGINS"),
        OsString::from(serde_json::to_string(protected)?),
    );
    Ok(vars)
}

/// Spawn the sibling desktop with the session environment. The owned process
/// environment is untouched; the child receives an explicit map.
pub fn launch(launch: &Launch) -> Result<()> {
    let mut vars = child_env(
        &snapshot(),
        &launch.root,
        &launch.host_id,
        &launch.protected,
    )?;
    vars.extend(launch.extra.iter().cloned());
    let status = Command::new(&launch.desktop_bin)
        .args(["--core"])
        .arg(&launch.core_bin)
        .args(["--config"])
        .arg(&launch.config_path)
        .arg(&launch.directory)
        .env_clear()
        .envs(&vars)
        .status()
        .with_context(|| format!("starting desktop {}", launch.desktop_bin.display()))?;
    if !status.success() {
        anyhow::bail!("desktop exited with {status}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{Launch, child_env, launch};
    use anyhow::Result;
    use std::collections::BTreeMap;
    use std::ffi::OsString;
    use std::path::{Path, PathBuf};

    fn expect_err<T>(result: Result<T>) -> Result<anyhow::Error> {
        match result {
            Ok(_) => anyhow::bail!("expected an error"),
            Err(error) => Ok(error),
        }
    }

    fn write_recorder(dir: &Path) -> Result<PathBuf> {
        let script = dir.join("tdnext-desktop");
        std::fs::write(
            &script,
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$TDNEXT_RECORD_ARGS\"\nprintenv > \"$TDNEXT_RECORD_ENV\"\n",
        )?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            let mut permissions = std::fs::metadata(&script)?.permissions();
            permissions.set_mode(0o755);
            std::fs::set_permissions(&script, permissions)?;
        }
        Ok(script)
    }

    #[test]
    fn launch_passes_session_environment_to_the_desktop() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let script = write_recorder(dir.path())?;
        let args_path = dir.path().join("args.txt");
        let env_path = dir.path().join("env.txt");
        let config_path = dir.path().join("desktop.json");
        std::fs::write(&config_path, r#"{"views":[]}"#)?;
        let live_path = std::env::var_os("PATH");
        launch(&Launch {
            desktop_bin: script,
            core_bin: dir.path().join("tdnext-core"),
            config_path: config_path.clone(),
            directory: dir.path().to_path_buf(),
            host_id: "desktop".to_string(),
            protected: vec!["desktop".to_string(), "settings".to_string()],
            root: dir.path().join("state"),
            extra: vec![
                (
                    OsString::from("TDNEXT_RECORD_ARGS"),
                    args_path.clone().into_os_string(),
                ),
                (
                    OsString::from("TDNEXT_RECORD_ENV"),
                    env_path.clone().into_os_string(),
                ),
            ],
        })?;
        let args = std::fs::read_to_string(&args_path)?;
        assert!(args.contains("--core"), "{args}");
        assert!(
            args.contains(config_path.to_str().unwrap_or_default()),
            "{args}"
        );
        let recorded: BTreeMap<String, String> = std::fs::read_to_string(&env_path)?
            .lines()
            .filter_map(|line| line.split_once('='))
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect();
        assert_eq!(
            recorded.get("TDNEXT_HOST_ID").map(String::as_str),
            Some("desktop")
        );
        assert_eq!(
            recorded.get("TDNEXT_PROTECTED_PLUGINS").map(String::as_str),
            Some(r#"["desktop","settings"]"#)
        );
        assert_eq!(
            recorded.get("TDNEXT_STATE_DIR").map(String::as_str),
            dir.path().join("state").to_str()
        );
        let live_path = live_path
            .as_deref()
            .map(|value| value.to_string_lossy().into_owned());
        assert_eq!(recorded.get("PATH"), live_path.as_ref());
        Ok(())
    }

    #[test]
    fn child_env_refuses_a_nested_session() -> Result<()> {
        let base: BTreeMap<OsString, OsString> = [(OsString::from("ZELLIJ"), OsString::from("0"))]
            .into_iter()
            .collect();
        let error = expect_err(child_env(&base, Path::new("/state"), "desktop", &[]))?;
        assert!(
            error.to_string().contains("existing Zellij session"),
            "{error:?}"
        );
        let clean: BTreeMap<OsString, OsString> =
            [(OsString::from("PATH"), OsString::from("/usr/bin"))]
                .into_iter()
                .collect();
        let vars = child_env(
            &clean,
            Path::new("/state"),
            "desktop",
            &["desktop".to_string()],
        )?;
        assert_eq!(
            vars.get(&OsString::from("TDNEXT_HOST_ID"))
                .map(|value| value.to_string_lossy().into_owned()),
            Some("desktop".to_string())
        );
        assert_eq!(
            vars.get(&OsString::from("TDNEXT_STATE_DIR"))
                .map(|value| value.to_string_lossy().into_owned()),
            Some("/state".to_string())
        );
        assert_eq!(
            vars.get(&OsString::from("PATH"))
                .map(|value| value.to_string_lossy().into_owned()),
            Some("/usr/bin".to_string())
        );
        Ok(())
    }
}
