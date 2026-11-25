use crate::preferences::{self, Appearance, Editor, IconMode};
use std::{
    collections::BTreeSet,
    path::PathBuf,
    process::Command,
    sync::mpsc::{self, Receiver, Sender},
    thread,
};
use tdnext_manager::{Action, Manager};

#[cfg(test)]
#[path = "app_tests.rs"]
mod tests;

#[derive(Clone)]
pub struct Plugin {
    pub id: String,
    pub name: String,
    pub description: String,
    pub role: String,
    pub installed: bool,
    pub enabled: bool,
    pub missing: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Section {
    General,
    Plugins,
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Provider,
    Style,
    Feedback,
    Editor,
    Back,
    General,
    Plugins,
    List,
    Install,
    Enable,
    Disable,
    Uninstall,
}
pub enum Dialog {
    Choice {
        focus: Focus,
        index: usize,
    },
    Confirm {
        text: String,
        action: Action,
        id: String,
        yes: bool,
    },
    Consent {
        text: String,
        reply: Sender<bool>,
        yes: bool,
    },
}
pub enum Message {
    Consent(String, Sender<bool>),
    Done(Result<Vec<String>, String>),
}

pub struct App {
    pub root: PathBuf,
    pub appearance: Option<Appearance>,
    pub editor: Option<Editor>,
    pub plugins: Vec<Plugin>,
    pub section: Section,
    pub focus: Focus,
    pub row: usize,
    pub list_top: usize,
    pub general_top: usize,
    pub feedback: String,
    pub feedback_top: u16,
    pub dialog: Option<Dialog>,
    pub busy: bool,
    pub protected: BTreeSet<String>,
    pub protection_valid: bool,
    pub tx: Sender<Message>,
    pub rx: Receiver<Message>,
}

impl App {
    pub fn new() -> anyhow::Result<Self> {
        let (tx, rx) = mpsc::channel();
        let mut protected = BTreeSet::from([
            "settings".to_owned(),
            std::env::var("TDNEXT_HOST_ID").unwrap_or_else(|_| "desktop".to_owned()),
        ]);
        let mut error = None;
        if let Ok(value) = std::env::var("TDNEXT_PROTECTED_PLUGINS") {
            match serde_json::from_str::<Vec<String>>(&value) {
                Ok(ids) => protected.extend(ids),
                Err(err) => error = Some(format!("Invalid TDNEXT_PROTECTED_PLUGINS: {err}")),
            }
        }
        let protection_valid = error.is_none();
        let mut app = Self {
            root: Manager::load()?.root,
            appearance: None,
            editor: None,
            plugins: Vec::new(),
            section: Section::General,
            focus: Focus::Provider,
            row: 0,
            list_top: 0,
            general_top: 0,
            feedback: error.unwrap_or_else(|| "Icons: restart. Editor: next file open.".into()),
            feedback_top: 0,
            dialog: None,
            busy: false,
            protected,
            protection_valid,
            tx,
            rx,
        };
        app.refresh();
        Ok(app)
    }

    pub fn refresh(&mut self) {
        match preferences::appearance(&self.root) {
            Ok(value) => self.appearance = Some(value),
            Err(e) => {
                self.appearance = None;
                self.feedback = format!("{e:#}. Repair the file to change appearance.");
            }
        }
        match preferences::editor(&self.root) {
            Ok(value) => self.editor = Some(value.editor),
            Err(e) => {
                self.editor = None;
                self.feedback = format!("{e:#}. Repair the file to change editor.");
            }
        }
        match Manager::load().and_then(|manager| {
            let state = manager.state()?;
            let mut ids: BTreeSet<_> = manager.manifests.keys().cloned().collect();
            ids.extend(state.installed.iter().cloned());
            ids.extend(manager.errors.keys().cloned());
            Ok(ids
                .into_iter()
                .map(|id| {
                    let manifest = manager.manifests.get(&id);
                    Plugin {
                        name: manifest.map_or_else(|| id.clone(), |m| m.name.clone()),
                        description: manifest.map_or_else(
                            || {
                                manager
                                    .errors
                                    .get(&id)
                                    .cloned()
                                    .unwrap_or_else(|| "Manifest missing".into())
                            },
                            |m| m.description.clone(),
                        ),
                        role: manifest.map_or_else(String::new, |m| m.role().to_owned()),
                        installed: state.installed.contains(&id),
                        enabled: state.enabled.contains(&id),
                        missing: manifest.is_none(),
                        id,
                    }
                })
                .collect())
        }) {
            Ok(plugins) => self.plugins = plugins,
            Err(e) => self.feedback = format!("Cannot load plugins: {e:#}"),
        }
        self.row = self.row.min(self.plugins.len().saturating_sub(1));
    }

    pub fn choices(&self, focus: Focus) -> Vec<(String, String)> {
        match focus {
            Focus::Provider => {
                let mut options = vec![("None (text)".into(), String::new())];
                let mut providers: Vec<_> = self
                    .plugins
                    .iter()
                    .filter(|p| p.enabled && p.role == "icons.provider")
                    .map(|p| (p.name.clone(), p.id.clone()))
                    .collect();
                providers.sort();
                options.extend(providers);
                if let Some(id) = self
                    .appearance
                    .as_ref()
                    .and_then(|a| a.provider_id.as_ref())
                    && !options.iter().any(|(_, value)| value == id)
                {
                    options.push((format!("{id} (unavailable)"), id.clone()));
                }
                options
            }
            Focus::Style => vec![
                ("Compatibility".into(), "compatibility".into()),
                ("Codicons".into(), "codicons".into()),
            ],
            Focus::Editor => vec![
                ("Environment".into(), "environment".into()),
                ("Neovim (nvim)".into(), "nvim".into()),
                ("Vi (vi)".into(), "vi".into()),
            ],
            _ => Vec::new(),
        }
    }

    pub fn current(&self, focus: Focus) -> String {
        match focus {
            Focus::Provider => self
                .appearance
                .as_ref()
                .and_then(|a| a.provider_id.clone())
                .unwrap_or_default(),
            Focus::Style => match self.appearance.as_ref().map(|a| a.icon_mode) {
                Some(IconMode::Codicons) => "codicons",
                _ => "compatibility",
            }
            .into(),
            Focus::Editor => match self.editor {
                Some(Editor::Nvim) => "nvim",
                Some(Editor::Vi) => "vi",
                _ => "environment",
            }
            .into(),
            _ => String::new(),
        }
    }

    pub fn choose(&mut self, focus: Focus, value: &str) {
        let result = (|| -> anyhow::Result<()> {
            match focus {
                Focus::Provider | Focus::Style => {
                    let mut appearance = preferences::appearance(&self.root)?;
                    let before = appearance.clone();
                    if focus == Focus::Provider {
                        appearance.provider_id = (!value.is_empty()).then(|| value.to_owned());
                    } else {
                        appearance.icon_mode = if value == "codicons" {
                            IconMode::Codicons
                        } else {
                            IconMode::Compatibility
                        };
                    }
                    if appearance != before {
                        preferences::save(&self.root.join("appearance.json"), &appearance)?;
                        self.feedback = "Appearance saved. Restart desktop to apply.".into();
                    }
                    self.appearance = Some(appearance);
                }
                Focus::Editor => {
                    let mut editor = preferences::editor(&self.root)?;
                    let before = editor.clone();
                    editor.editor = match value {
                        "nvim" => Editor::Nvim,
                        "vi" => Editor::Vi,
                        _ => Editor::Environment,
                    };
                    if editor != before {
                        preferences::save(&self.root.join("editor.json"), &editor)?;
                        self.feedback = "Editor saved. Applies on the next file open.".into();
                    }
                    self.editor = Some(editor.editor);
                }
                _ => {}
            }
            Ok(())
        })();
        if let Err(error) = result {
            self.refresh();
            self.feedback = format!("Cannot save preference: {error:#}");
        }
    }

    pub fn action_available(&self, action: Action) -> bool {
        let Some(plugin) = self.plugins.get(self.row) else {
            return false;
        };
        if self.busy || !self.protection_valid {
            return false;
        }
        match action {
            Action::Install => !plugin.installed && !plugin.missing,
            Action::Enable => plugin.installed && !plugin.enabled && !plugin.missing,
            Action::Disable => plugin.enabled && !self.protected.contains(&plugin.id),
            Action::Uninstall => plugin.installed && !self.protected.contains(&plugin.id),
        }
    }

    pub fn request(&mut self, action: Action) {
        if !self.protection_valid {
            self.feedback =
                "Invalid TDNEXT_PROTECTED_PLUGINS; plugin operations are disabled.".into();
            return;
        }
        if !self.action_available(action) {
            return;
        }
        let Some(id) = self.plugins.get(self.row).map(|plugin| plugin.id.clone()) else {
            return;
        };
        if matches!(action, Action::Disable | Action::Uninstall) {
            match Manager::load().and_then(|m| m.affected(&id)) {
                Ok(affected) => {
                    let dependants: Vec<_> = affected
                        .iter()
                        .filter(|item| *item != &id)
                        .cloned()
                        .collect();
                    if matches!(action, Action::Uninstall) || !dependants.is_empty() {
                        self.dialog = Some(Dialog::Confirm {
                            text: format!(
                                "{} {}?\nAlso affected: {}",
                                if matches!(action, Action::Uninstall) {
                                    "Uninstall"
                                } else {
                                    "Disable"
                                },
                                id,
                                if dependants.is_empty() {
                                    "none".into()
                                } else {
                                    dependants.join(", ")
                                }
                            ),
                            action,
                            id,
                            yes: false,
                        });
                        self.feedback_top = 0;
                        return;
                    }
                }
                Err(error) => {
                    self.feedback = format!("Cannot preview change: {error:#}");
                    return;
                }
            }
        }
        self.start(action, id);
    }

    pub fn start(&mut self, action: Action, id: String) {
        self.busy = true;
        self.feedback = format!("Working on {id}... (operations cannot run concurrently)");
        let tx = self.tx.clone();
        let protected = self.protected.clone();
        thread::spawn(move || {
            let result = Manager::load()
                .and_then(|manager| {
                    manager.apply(action, &id, &protected, |prompt| {
                        let (reply, response) = mpsc::channel();
                        if tx.send(Message::Consent(prompt.to_owned(), reply)).is_err() {
                            return false;
                        }
                        response.recv().unwrap_or(false)
                    })
                })
                .map_err(|error| format!("{error:#}"));
            if let Err(error) = tx.send(Message::Done(result)) {
                eprintln!("Cannot deliver operation result: {error}");
            }
        });
    }

    pub fn poll(&mut self) {
        while let Ok(message) = self.rx.try_recv() {
            match message {
                Message::Consent(text, reply) => {
                    self.feedback_top = 0;
                    self.dialog = Some(Dialog::Consent {
                        text,
                        reply,
                        yes: false,
                    })
                }
                Message::Done(result) => {
                    self.busy = false;
                    self.refresh();
                    self.feedback = match result {
                        Ok(lines) => format!(
                            "{}\nRestart desktop to apply plugin changes.",
                            lines.join("\n")
                        ),
                        Err(error) => format!("Operation failed: {error}"),
                    };
                }
            }
        }
    }

    pub fn back(&mut self) {
        let result = std::env::var_os("TDNEXT_ZELLIJ")
            .ok_or_else(|| "TDNEXT_ZELLIJ is not set".to_owned())
            .and_then(|binary| {
                Command::new(binary)
                    .args(["action", "return-to-origin"])
                    .output()
                    .map_err(|e| e.to_string())
            })
            .and_then(|output| {
                if output.status.success() {
                    Ok(())
                } else {
                    Err(String::from_utf8_lossy(&output.stderr).into_owned())
                }
            });
        if let Err(error) = result {
            self.feedback = format!("Cannot return to origin: {error}");
        }
    }
}
