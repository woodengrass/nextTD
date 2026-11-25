use super::{App, Focus, Section};
use crate::preferences::{self, Appearance, IconMode};
use std::{collections::BTreeSet, sync::mpsc};

fn general_app(root: &std::path::Path) -> App {
    let (tx, rx) = mpsc::channel();
    App {
        root: root.to_path_buf(),
        appearance: Some(Appearance::default()),
        editor: None,
        plugins: Vec::new(),
        section: Section::General,
        focus: Focus::Provider,
        row: 0,
        list_top: 0,
        general_top: 0,
        general_size: (0, 0),
        feedback: String::new(),
        feedback_top: 0,
        dialog: None,
        busy: false,
        protected: BTreeSet::new(),
        protection_valid: true,
        tx,
        rx,
    }
}

#[test]
fn general_heading_after_resize_matches_direct_start() {
    fn body_top(app: &mut App, w: u16, h: u16) -> String {
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
        terminal
            .draw(|frame| {
                crate::ui::draw(frame, app);
            })
            .unwrap();
        (0..w)
            .map(|x| terminal.backend().buffer()[(x, 6)].symbol())
            .collect()
    }
    let dir = tempfile::tempdir().unwrap();
    let mut app = general_app(dir.path());
    let expected = body_top(&mut app, 98, 36);
    assert!(expected.contains("Icon appearance"));
    let mut resized = general_app(dir.path());
    body_top(&mut resized, 60, 10);
    assert_eq!(body_top(&mut resized, 98, 36), expected);
}

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
    let mut app = general_app(dir.path());
    app.choose(Focus::Provider, "icons");
    let saved = preferences::appearance(dir.path()).unwrap();
    assert_eq!(saved.provider_id.as_deref(), Some("icons"));
    assert_eq!(saved.icon_mode, IconMode::Codicons);
}
