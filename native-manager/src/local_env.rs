//! Shared-process environment helpers for the native CLI.
//!
//! The owned process environment is never mutated (mutating it is `unsafe`
//! in current Rust, and this crate forbids `unsafe`); instead, panes and
//! child processes receive explicit maps. The interactive shell runs with
//! the user's original PATH and no bootstrap metadata, so user-facing
//! surfaces always see the exact environment they started with.

use std::collections::BTreeMap;
use std::env;
use std::ffi::{OsStr, OsString};

pub const STATE_DIR_ENV: &str = "TDNEXT_STATE_DIR";
pub const BOOTSTRAPPED_ENV: &str = "TDNEXT_PATH_BOOTSTRAPPED";
pub const ORIGINAL_ENV: &str = "TDNEXT_ORIGINAL_PATH";
pub const ORIGINAL_PRESENT_ENV: &str = "TDNEXT_ORIGINAL_PATH_PRESENT";

const PATH_ENV: &str = "PATH";

fn get<'a>(vars: &'a BTreeMap<OsString, OsString>, key: &str) -> Option<&'a OsString> {
    vars.get(OsStr::new(key))
}

/// Accept bootstrap metadata only when complete and self-consistent.
fn markers_valid(vars: &BTreeMap<OsString, OsString>) -> bool {
    if get(vars, BOOTSTRAPPED_ENV).is_none_or(|value| value != "1") {
        return false;
    }
    match get(vars, ORIGINAL_PRESENT_ENV).map(OsString::as_os_str) {
        Some(value) if value == "0" => true,
        Some(value) if value == "1" => get(vars, ORIGINAL_ENV).is_some(),
        _ => false,
    }
}

/// Copy a map with PATH restored and bootstrap metadata removed.
pub fn restore_vars(vars: &mut BTreeMap<OsString, OsString>) {
    let (present, value) = if markers_valid(vars) {
        (
            get(vars, ORIGINAL_PRESENT_ENV).is_some_and(|value| value == "1"),
            get(vars, ORIGINAL_ENV).cloned(),
        )
    } else {
        (
            vars.contains_key(OsStr::new(PATH_ENV)),
            vars.get(OsStr::new(PATH_ENV)).cloned(),
        )
    };
    match (present, value) {
        (true, Some(value)) => {
            vars.insert(OsString::from(PATH_ENV), value);
        }
        _ => {
            vars.remove(OsStr::new(PATH_ENV));
        }
    }
    vars.remove(OsStr::new(BOOTSTRAPPED_ENV));
    vars.remove(OsStr::new(ORIGINAL_ENV));
    vars.remove(OsStr::new(ORIGINAL_PRESENT_ENV));
}

/// Snapshot of the live process environment as an owned map.
#[must_use]
pub fn snapshot() -> BTreeMap<OsString, OsString> {
    env::vars_os().collect()
}

#[cfg(test)]
mod tests {
    use super::restore_vars;
    use anyhow::{Context, Result};
    use std::collections::BTreeMap;
    use std::ffi::OsString;

    fn vars(pairs: &[(&str, &str)]) -> BTreeMap<OsString, OsString> {
        pairs
            .iter()
            .map(|(key, value)| (OsString::from(key), OsString::from(value)))
            .collect()
    }

    fn path_of(vars: &BTreeMap<OsString, OsString>) -> Result<String> {
        vars.get(&OsString::from("PATH"))
            .map(|value| value.to_string_lossy().into_owned())
            .with_context(|| "PATH must be present")
    }

    #[test]
    fn restore_returns_the_original_path() -> Result<()> {
        let mut live = vars(&[
            ("PATH", "/state/bin:/usr/bin"),
            ("TDNEXT_PATH_BOOTSTRAPPED", "1"),
            ("TDNEXT_ORIGINAL_PATH_PRESENT", "1"),
            ("TDNEXT_ORIGINAL_PATH", "/usr/bin"),
        ]);
        restore_vars(&mut live);
        assert_eq!(path_of(&live)?, "/usr/bin");
        assert!(
            !live
                .keys()
                .any(|key| key.to_string_lossy().starts_with("TDNEXT_"))
        );
        Ok(())
    }

    #[test]
    fn restore_drops_a_path_captured_absent() -> Result<()> {
        let mut live = vars(&[
            ("PATH", "/state/bin"),
            ("TDNEXT_PATH_BOOTSTRAPPED", "1"),
            ("TDNEXT_ORIGINAL_PATH_PRESENT", "0"),
        ]);
        restore_vars(&mut live);
        assert!(!live.contains_key(&OsString::from("PATH")));
        Ok(())
    }

    #[test]
    fn restore_ignores_forged_markers() -> Result<()> {
        let mut live = vars(&[
            ("PATH", "/usr/bin"),
            ("TDNEXT_PATH_BOOTSTRAPPED", "1"),
            ("TDNEXT_ORIGINAL_PATH_PRESENT", "yes"),
        ]);
        restore_vars(&mut live);
        assert_eq!(path_of(&live)?, "/usr/bin");
        assert!(
            !live
                .keys()
                .any(|key| key.to_string_lossy().starts_with("TDNEXT_"))
        );
        Ok(())
    }
}
