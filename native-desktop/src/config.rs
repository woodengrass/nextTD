use std::{collections::HashSet, env, fs, path::Path};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub views: Vec<View>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct View {
    pub id: String,
    pub name: String,
    #[serde(default = "default_icon")]
    pub icon: String,
    #[serde(default)]
    pub shortcuts: String,
    pub placement: Placement,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default = "enabled")]
    pub enabled: bool,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Placement {
    Sidebar,
    Header,
}

fn default_icon() -> String {
    "[]".into()
}

const fn enabled() -> bool {
    true
}

impl Config {
    pub fn load(path: Option<&Path>) -> Result<Self> {
        let mut config = match path {
            Some(path) => serde_json::from_slice::<Self>(
                &fs::read(path).with_context(|| format!("reading {}", path.display()))?,
            )
            .with_context(|| format!("parsing {}", path.display()))?,
            None => Self {
                views: vec![View {
                    id: "shell".into(),
                    name: "Shell".into(),
                    icon: ">_".into(),
                    shortcuts: String::new(),
                    placement: Placement::Sidebar,
                    command: env::var("SHELL").unwrap_or_else(|_| "/bin/sh".into()),
                    args: vec![],
                    enabled: true,
                }],
            },
        };
        config.views.retain(|view| view.enabled);
        config.views.sort_by_key(|view| match view.placement {
            Placement::Sidebar => 0,
            Placement::Header => 1,
        });
        let mut ids = HashSet::new();
        let mut tabs = HashSet::new();
        for view in &config.views {
            if view.id.is_empty() || view.name.is_empty() || view.command.is_empty() {
                bail!("view id, name, and command must not be empty");
            }
            if !ids.insert(&view.id) || !tabs.insert(view.tab_name()) {
                bail!("view ids and tab names must be unique");
            }
            if std::iter::once(&view.id)
                .chain([&view.name, &view.icon, &view.shortcuts, &view.command])
                .chain(&view.args)
                .any(|value| value.chars().any(char::is_control))
            {
                bail!("view fields cannot contain control characters");
            }
        }
        Ok(config)
    }
}

impl View {
    pub fn tab_name(&self) -> String {
        match self.placement {
            Placement::Sidebar => self.name.clone(),
            Placement::Header => format!("tdnext-header:{}", self.id),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Config;
    use anyhow::Result;
    use std::fs;

    #[test]
    fn explicit_configuration_can_have_no_enabled_views() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("config.json");
        for config in [
            r#"{"views":[]}"#,
            r#"{"views":[{"id":"x","name":"X","placement":"sidebar","command":"/bin/sh","enabled":false}]}"#,
        ] {
            fs::write(&path, config)?;
            assert!(Config::load(Some(&path))?.views.is_empty());
        }
        Ok(())
    }
}
