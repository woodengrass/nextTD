use anyhow::{Context, Result};
use serde::Serialize;

use crate::config::{Config, Placement, View};

#[derive(Serialize)]
struct Chrome<'a> {
    id: &'a str,
    name: &'a str,
    icon: &'a str,
    shortcuts: &'a str,
    placement: Placement,
    tab_name: String,
}

pub fn chrome(config: &Config) -> Result<Vec<u8>> {
    let entries: Vec<_> = config
        .views
        .iter()
        .map(|view| Chrome {
            id: &view.id,
            name: &view.name,
            icon: &view.icon,
            shortcuts: &view.shortcuts,
            placement: view.placement,
            tab_name: view.tab_name(),
        })
        .collect();
    Ok(serde_json::to_vec(&entries)?)
}

fn quoted(value: &str) -> Result<String> {
    Ok(serde_json::to_string(value)?)
}

pub fn layout(config: &Config) -> Result<String> {
    let mut text = String::from("layout {\n");
    if config.views.is_empty() {
        let executable = std::env::current_exe()?;
        let path = executable.to_str().context("launcher path is not UTF-8")?;
        text.push_str(&format!(
            "tab name=\"Desktop\" focus=true {{\n pane focus=true borderless=true command={} {{\n  args \"--internal-empty-pane\"\n }}\n}}\n",
            quoted(path)?
        ));
    }
    for (index, view) in config.views.iter().enumerate() {
        let focus = if index == 0 { " focus=true" } else { "" };
        text.push_str(&format!(
            "tab name={}{focus} {{\n pane focus=true borderless=true command={} {{\n",
            quoted(&view.tab_name())?,
            quoted(&view.command)?
        ));
        if !view.args.is_empty() {
            text.push_str("  args");
            for arg in &view.args {
                text.push(' ');
                text.push_str(&quoted(arg)?);
            }
            text.push('\n');
        }
        text.push_str(" }\n}\n");
    }
    text.push_str("}\n");
    Ok(text)
}

pub fn settings(config: &Config) -> Result<String> {
    let mut text = String::from(include_str!("theme.kdl"));
    text.push_str("\nkeybinds {\n shared_except \"locked\" {\n");
    let count = config
        .views
        .iter()
        .filter(|v| v.placement == Placement::Sidebar)
        .count();
    for index in 1..=9 {
        if index <= count {
            text.push_str(&format!("  bind \"Alt {index}\" {{ GoToTab {index}; }}\n"));
        } else {
            text.push_str(&format!("  unbind \"Alt {index}\"\n"));
        }
    }
    match config
        .views
        .iter()
        .find(|view: &&View| view.placement == Placement::Header)
    {
        Some(view) => text.push_str(&format!(
            "  bind \"Alt o\" {{ OpenTabWithReturn {}; }}\n",
            quoted(&view.tab_name())?
        )),
        None => text.push_str("  unbind \"Alt o\"\n"),
    }
    text.push_str(" }\n}\n");
    Ok(text)
}
