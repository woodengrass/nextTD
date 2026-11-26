use std::{fs, io::ErrorKind, path::Path};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize, de::DeserializeOwned};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct Appearance {
    pub provider_id: Option<String>,
    pub icon_mode: IconMode,
}

impl Default for Appearance {
    fn default() -> Self {
        Self {
            provider_id: None,
            icon_mode: IconMode::Compatibility,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum IconMode {
    #[default]
    Compatibility,
    Codicons,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Editor {
    #[default]
    Environment,
    Nvim,
    Vi,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct EditorPreferences {
    pub editor: Editor,
}

fn read<T: DeserializeOwned + Default>(path: &Path) -> Result<T> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .with_context(|| format!("Cannot read preferences {}", path.display())),
        Err(error) if error.kind() == ErrorKind::NotFound && !path.is_symlink() => Ok(T::default()),
        Err(error) => {
            Err(error).with_context(|| format!("Cannot read preferences {}", path.display()))
        }
    }
}

pub fn appearance(root: &Path) -> Result<Appearance> {
    read_appearance(&root.join("appearance.json"))
}

pub fn read_appearance(path: &Path) -> Result<Appearance> {
    let value: Appearance = read(path)?;
    if let Some(id) = &value.provider_id {
        let mut chars = id.chars();
        if !chars.next().is_some_and(|c| c.is_ascii_lowercase())
            || !chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
        {
            bail!(
                "Cannot read preferences {}: invalid provider id",
                path.display()
            );
        }
    }
    Ok(value)
}

pub fn editor(root: &Path) -> Result<EditorPreferences> {
    read_editor(&root.join("editor.json"))
}

pub fn read_editor(path: &Path) -> Result<EditorPreferences> {
    read(path)
}

#[cfg(test)]
pub fn save<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    tdnext_manager::state::StateStore::new(path.to_owned())
        .lock()?
        .replace(value)?
        .sync()
}
