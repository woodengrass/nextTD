use crate::{
    app::{App, Dialog, Focus, Section},
    ui::{Hit, Targets},
};
use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind};
use tdnext_manager::Action;

fn cycle(app: &mut App, reverse: bool) {
    let order: &[Focus] = match app.section {
        Section::General => &[
            Focus::Provider,
            Focus::Style,
            Focus::Feedback,
            Focus::Editor,
            Focus::Back,
            Focus::General,
            Focus::Plugins,
        ],
        Section::Plugins => &[
            Focus::List,
            Focus::Install,
            Focus::Enable,
            Focus::Disable,
            Focus::Uninstall,
            Focus::Feedback,
            Focus::Back,
            Focus::General,
            Focus::Plugins,
        ],
    };
    let index = order
        .iter()
        .position(|focus| *focus == app.focus)
        .unwrap_or(0);
    app.focus = order[(index + if reverse { order.len() - 1 } else { 1 }) % order.len()];
    if matches!(app.focus, Focus::Provider | Focus::Style | Focus::Editor) {
        app.refresh();
    }
}
fn activate(app: &mut App, focus: Focus) {
    app.focus = focus;
    match focus {
        Focus::Back => app.back(),
        Focus::General => {
            app.section = Section::General;
            app.focus = Focus::Provider;
            app.feedback = "Icons: restart. Editor: next file open.".into();
            app.refresh();
        }
        Focus::Plugins => {
            app.section = Section::Plugins;
            app.focus = Focus::List;
            app.feedback = "Changes apply after restarting the desktop.".into();
            app.refresh();
        }
        Focus::Provider | Focus::Style | Focus::Editor => {
            app.refresh();
            if (focus == Focus::Editor && app.editor.is_none())
                || (focus != Focus::Editor && app.appearance.is_none())
            {
                return;
            }
            let current = app.current(focus);
            let index = app
                .choices(focus)
                .iter()
                .position(|(_, value)| *value == current)
                .unwrap_or(0);
            app.dialog = Some(Dialog::Choice { focus, index });
        }
        Focus::Install => app.request(Action::Install),
        Focus::Enable => app.request(Action::Enable),
        Focus::Disable => app.request(Action::Disable),
        Focus::Uninstall => app.request(Action::Uninstall),
        Focus::Feedback | Focus::List => {}
    }
}
fn confirm(app: &mut App, yes: bool) {
    if let Some(dialog) = app.dialog.take() {
        match dialog {
            Dialog::Choice { focus, index } => {
                if yes && let Some((_, value)) = app.choices(focus).get(index).cloned() {
                    app.choose(focus, &value);
                }
            }
            Dialog::Confirm {
                action,
                id,
                affected,
                ..
            } => {
                if yes {
                    app.start(action, id, Some(affected));
                }
            }
            Dialog::Consent { reply, .. } => {
                if let Err(error) = reply.send(yes) {
                    app.feedback = format!("Cannot send consent: {error}");
                }
            }
        }
    }
}
pub fn handle(app: &mut App, event: Event, targets: &Targets) -> bool {
    match event {
        Event::FocusGained => {
            app.refresh();
        }
        Event::Key(key) if key.kind == KeyEventKind::Press => {
            if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('q') {
                if app.busy {
                    app.feedback =
                        "Plugin operation in progress. Finish or cancel it before quitting.".into();
                    return false;
                }
                if app.dialog.is_some() {
                    confirm(app, false);
                }
                return true;
            }
            if let Some(dialog) = &mut app.dialog {
                match key.code {
                    KeyCode::Esc => confirm(app, false),
                    KeyCode::Enter => {
                        let yes = match dialog {
                            Dialog::Choice { .. } => true,
                            Dialog::Confirm { yes, .. } | Dialog::Consent { yes, .. } => *yes,
                        };
                        confirm(app, yes);
                    }
                    KeyCode::Left | KeyCode::Right | KeyCode::Tab | KeyCode::BackTab => {
                        if let Dialog::Confirm { yes, .. } | Dialog::Consent { yes, .. } = dialog {
                            *yes = !*yes;
                        }
                    }
                    KeyCode::Up => match dialog {
                        Dialog::Choice { index, .. } => *index = index.saturating_sub(1),
                        _ => app.feedback_top = app.feedback_top.saturating_sub(1),
                    },
                    KeyCode::Down => match dialog {
                        Dialog::Choice { focus, index } => {
                            let focus = *focus;
                            let index = *index;
                            let next = (index + 1).min(app.choices(focus).len().saturating_sub(1));
                            if let Some(Dialog::Choice { index, .. }) = &mut app.dialog {
                                *index = next;
                            }
                        }
                        _ => app.feedback_top = app.feedback_top.saturating_add(1),
                    },
                    _ => {}
                }
                return false;
            }
            match key.code {
                KeyCode::Esc => app.back(),
                KeyCode::Tab => cycle(app, false),
                KeyCode::BackTab => cycle(app, true),
                KeyCode::Enter | KeyCode::Char(' ') => activate(app, app.focus),
                KeyCode::Up => {
                    if app.focus == Focus::List {
                        app.row = app.row.saturating_sub(1);
                    } else if app.focus == Focus::Feedback {
                        app.feedback_top = app.feedback_top.saturating_sub(1);
                    }
                }
                KeyCode::Down => {
                    if app.focus == Focus::List {
                        app.row = (app.row + 1).min(app.plugins.len().saturating_sub(1));
                    } else if app.focus == Focus::Feedback {
                        app.feedback_top = app.feedback_top.saturating_add(1);
                    }
                }
                KeyCode::Char('i') if app.section == Section::Plugins => {
                    app.request(Action::Install)
                }
                KeyCode::Char('e') if app.section == Section::Plugins => {
                    app.request(Action::Enable)
                }
                KeyCode::Char('d') if app.section == Section::Plugins => {
                    app.request(Action::Disable)
                }
                KeyCode::Char('u') if app.section == Section::Plugins => {
                    app.request(Action::Uninstall)
                }
                _ => {}
            }
        }
        Event::Mouse(mouse) => match mouse.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                if let Some(hit) = targets.at(mouse.column, mouse.row) {
                    if app.dialog.is_some() {
                        match hit {
                            Hit::Choice(i) => {
                                if let Some(Dialog::Choice { index, .. }) = &mut app.dialog {
                                    *index = i;
                                    confirm(app, true);
                                }
                            }
                            Hit::Cancel => confirm(app, false),
                            Hit::Confirm => confirm(app, true),
                            _ => {}
                        }
                    } else {
                        match hit {
                            Hit::Focus(focus) => activate(app, focus),
                            Hit::Row(row) => {
                                app.row = row;
                                app.focus = Focus::List;
                            }
                            _ => {}
                        }
                    }
                }
            }
            MouseEventKind::ScrollUp => {
                if app.dialog.is_some() || app.focus == Focus::Feedback {
                    app.feedback_top = app.feedback_top.saturating_sub(1);
                } else if app.section == Section::Plugins {
                    app.row = app.row.saturating_sub(1);
                } else {
                    app.general_top = app.general_top.saturating_sub(1);
                }
            }
            MouseEventKind::ScrollDown => {
                if app.dialog.is_some() || app.focus == Focus::Feedback {
                    app.feedback_top = app.feedback_top.saturating_add(1);
                } else if app.section == Section::Plugins {
                    app.row = (app.row + 1).min(app.plugins.len().saturating_sub(1));
                } else {
                    app.general_top = app.general_top.saturating_add(1);
                }
            }
            _ => {}
        },
        _ => {}
    }
    false
}
