use super::{App, Focus, Section};
use crate::preferences::{self, Appearance, IconMode};
use std::{collections::BTreeSet, sync::mpsc};

#[test]
fn changing_provider_preserves_the_latest_saved_style() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("appearance.json");
    preferences::save(
        &path,
        &Appearance {
            provider_id: None,
            icon_mode: IconMode::Codicons,
        },
    )
    .unwrap();
    let (tx, rx) = mpsc::channel();
    let mut app = App {
        root: dir.path().to_path_buf(),
        appearance: Some(Appearance::default()),
        editor: None,
        plugins: Vec::new(),
        section: Section::General,
        focus: Focus::Provider,
        row: 0,
        list_top: 0,
        general_top: 0,
        feedback: String::new(),
        feedback_top: 0,
        dialog: None,
        busy: false,
        protected: BTreeSet::new(),
        protection_valid: true,
        tx,
        rx,
    };
    app.choose(Focus::Provider, "icons");
    let saved = preferences::appearance(dir.path()).unwrap();
    assert_eq!(saved.provider_id.as_deref(), Some("icons"));
    assert_eq!(saved.icon_mode, IconMode::Codicons);
}
