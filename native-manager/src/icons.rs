//! Icon maps with attribution, honoring the selected provider appearance.
//!
//! Compatibility glyphs use plain text. The Codicons map follows Microsoft
//! Codicons 0.0.45 as mapped by Nerd Fonts 3.5.1
//! (https://github.com/ryanoasis/nerd-fonts/tree/v3.5.1), licensed under
//! CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); a compatible
//! Nerd Font must be selected in the terminal to display them.

use std::path::Path;

const COMPATIBILITY: &[(&str, &str)] = &[
    ("files", "[F]"),
    ("monitor", "[M]"),
    ("shell", ">_"),
    ("settings", "[S]"),
];

const CODICONS: &[(&str, &str)] = &[
    ("files", "\u{ea83}"),
    ("monitor", "\u{eb31}"),
    ("shell", "\u{ea85}"),
    ("settings", "\u{eb52}"),
];

const FALLBACK: &str = "[]";

/// Appearance selection stored beside plugin state (`appearance.json`).
pub struct Appearance {
    pub provider_id: Option<String>,
    pub codicons: bool,
}

fn is_provider_id(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .next()
            .is_some_and(|first| first.is_ascii_lowercase())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-'
        })
}

/// Read `<root>/appearance.json`; a missing file means compatibility text.
pub fn read_appearance(root: &Path) -> anyhow::Result<Appearance> {
    let path = root.join("appearance.json");
    let text = match std::fs::read_to_string(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(Appearance {
                provider_id: None,
                codicons: false,
            });
        }
        Err(error) => {
            return Err(anyhow::anyhow!(
                "Cannot read appearance {}: {error}",
                path.display()
            ));
        }
        Ok(text) => text,
    };
    let value: serde_json::Value = serde_json::from_str(&text)
        .map_err(|error| anyhow::anyhow!("Cannot read appearance {}: {error}", path.display()))?;
    let object = value.as_object().ok_or_else(|| {
        anyhow::anyhow!(
            "Cannot read appearance {}: expected an object",
            path.display()
        )
    })?;
    for key in object.keys() {
        if key != "provider_id" && key != "icon_mode" {
            return Err(anyhow::anyhow!(
                "Cannot read appearance {}: unexpected field {key}",
                path.display()
            ));
        }
    }
    let provider_id = match object.get("provider_id") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(id)) if is_provider_id(id) => Some(id.clone()),
        Some(other) => {
            return Err(anyhow::anyhow!(
                "Cannot read appearance {}: invalid provider_id {other}",
                path.display()
            ));
        }
    };
    let codicons = match object.get("icon_mode") {
        None => false,
        Some(serde_json::Value::String(mode)) if mode == "compatibility" => false,
        Some(serde_json::Value::String(mode)) if mode == "codicons" => true,
        Some(other) => {
            return Err(anyhow::anyhow!(
                "Cannot read appearance {}: invalid icon_mode {other}",
                path.display()
            ));
        }
    };
    Ok(Appearance {
        provider_id,
        codicons,
    })
}

/// Resolve one icon: provider map hit, else the manifest icon, else `[]`.
#[must_use]
pub fn resolve(key: &str, codicons: bool, fallback: Option<&str>) -> String {
    let table = if codicons { CODICONS } else { COMPATIBILITY };
    table
        .iter()
        .find(|(name, _)| *name == key)
        .map(|(_, glyph)| (*glyph).to_string())
        .unwrap_or_else(|| fallback.unwrap_or(FALLBACK).to_string())
}

#[cfg(test)]
mod tests {
    use super::{read_appearance, resolve};
    use anyhow::Result;

    #[test]
    fn compatibility_map_serves_plain_text() -> Result<()> {
        assert_eq!(resolve("files", false, None), "[F]");
        assert_eq!(resolve("shell", false, None), ">_");
        assert_eq!(resolve("unknown", false, None), "[]");
        assert_eq!(resolve("unknown", false, Some("X")), "X");
        Ok(())
    }

    #[test]
    fn codicons_map_serves_nerd_font_glyphs() -> Result<()> {
        assert_eq!(resolve("files", true, None), "\u{ea83}");
        assert_eq!(resolve("settings", true, Some("[S]")), "\u{eb52}");
        assert_eq!(resolve("unknown", true, None), "[]");
        Ok(())
    }

    #[test]
    fn missing_appearance_means_compatibility_text() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let appearance = read_appearance(dir.path())?;
        assert!(appearance.provider_id.is_none());
        assert!(!appearance.codicons);
        Ok(())
    }

    #[test]
    fn codicons_selection_parses() -> Result<()> {
        let dir = tempfile::tempdir()?;
        std::fs::write(
            dir.path().join("appearance.json"),
            r#"{"provider_id":"icons","icon_mode":"codicons"}"#,
        )?;
        let appearance = read_appearance(dir.path())?;
        assert_eq!(appearance.provider_id.as_deref(), Some("icons"));
        assert!(appearance.codicons);
        Ok(())
    }

    #[test]
    fn corrupt_appearance_is_an_error() -> Result<()> {
        let dir = tempfile::tempdir()?;
        for body in [
            "{not json",
            r#"{"icon_mode":"emoji"}"#,
            r#"{"provider_id":"Icons"}"#,
            r#"{"provider_id":"icons","icon_mode":"codicons","extra":1}"#,
            r#"{"provider_id":7}"#,
        ] {
            std::fs::write(dir.path().join("appearance.json"), body)?;
            let error = match read_appearance(dir.path()) {
                Ok(_) => anyhow::bail!("expected an error for {body}"),
                Err(error) => error,
            };
            assert!(
                error.to_string().contains("Cannot read appearance"),
                "{error:?}"
            );
        }
        Ok(())
    }
}
