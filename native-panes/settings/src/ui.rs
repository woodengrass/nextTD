use crate::app::{App, Dialog, Focus, Section};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::Span,
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
};
use tdnext_manager::Action;

#[derive(Clone, Copy)]
pub enum Hit {
    Focus(Focus),
    Row(usize),
    Choice(usize),
    Cancel,
    Confirm,
}
pub struct Targets(pub Vec<(Rect, Hit)>);
impl Targets {
    pub fn at(&self, x: u16, y: u16) -> Option<Hit> {
        self.0
            .iter()
            .rev()
            .find(|(r, _)| x >= r.x && x < r.right() && y >= r.y && y < r.bottom())
            .map(|(_, hit)| *hit)
    }
}
fn control(
    frame: &mut Frame,
    targets: &mut Targets,
    rect: Rect,
    text: &str,
    _focused: bool,
    hit: Hit,
) {
    let background = Color::Black;
    let style = Style::default()
        .fg(Color::White)
        .add_modifier(Modifier::BOLD);
    frame.render_widget(
        Block::default()
            .borders(Borders::ALL)
            .style(Style::default().fg(Color::Gray).bg(background)),
        rect,
    );
    frame.render_widget(
        Paragraph::new(Span::styled(format!(" {text} "), style))
            .style(Style::default().fg(Color::Reset).bg(background))
            .alignment(ratatui::layout::Alignment::Center),
        rect.inner(ratatui::layout::Margin {
            vertical: 1,
            horizontal: 1,
        }),
    );
    targets.0.push((rect, hit));
}
pub fn draw(frame: &mut Frame, app: &mut App) -> Targets {
    let mut targets = Targets(Vec::new());
    let area = frame.area();
    frame.render_widget(
        Block::default().style(Style::default().bg(Color::Black)),
        area,
    );
    let area = Rect::new(
        area.x.saturating_add(1),
        area.y,
        area.width.saturating_sub(2),
        area.height,
    );
    let [header, tabs, body, feedback] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(3),
        Constraint::Min(1),
        Constraint::Length(
            if app.feedback.contains('\n')
                || app.feedback.chars().count() > usize::from(area.width.saturating_sub(2))
            {
                4
            } else {
                3
            },
        ),
    ])
    .areas(area);
    let [title, back] =
        Layout::horizontal([Constraint::Min(1), Constraint::Length(12)]).areas(header);
    frame.render_widget(
        Paragraph::new(Span::styled(
            "Settings",
            Style::default()
                .fg(Color::White)
                .add_modifier(Modifier::BOLD),
        )),
        title,
    );
    control(
        frame,
        &mut targets,
        back,
        "Back",
        app.focus == Focus::Back,
        Hit::Focus(Focus::Back),
    );
    let [general, plugins, _] = Layout::horizontal([
        Constraint::Length(15),
        Constraint::Length(15),
        Constraint::Min(0),
    ])
    .areas(tabs);
    control(
        frame,
        &mut targets,
        general,
        if app.section == Section::General {
            "> General"
        } else {
            "  General"
        },
        app.focus == Focus::General,
        Hit::Focus(Focus::General),
    );
    control(
        frame,
        &mut targets,
        plugins,
        if app.section == Section::Plugins {
            "> Plugins"
        } else {
            "  Plugins"
        },
        app.focus == Focus::Plugins,
        Hit::Focus(Focus::Plugins),
    );
    match app.section {
        Section::General => draw_general(frame, app, body, &mut targets),
        Section::Plugins => draw_plugins(frame, app, body, &mut targets),
    }
    let feedback_style = if app.focus == Focus::Feedback {
        Style::default()
            .bg(Color::Black)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default()
    };
    frame.render_widget(
        Paragraph::new(Span::styled(
            app.feedback.as_str(),
            Style::default().fg(Color::White),
        ))
        .wrap(Wrap { trim: false })
        .scroll((app.feedback_top, 0))
        .style(feedback_style)
        .block(Block::bordered().border_style(Style::default().fg(Color::Gray))),
        feedback,
    );
    targets.0.push((feedback, Hit::Focus(Focus::Feedback)));
    if let Some(dialog) = &app.dialog
        && !matches!(
            dialog,
            Dialog::Choice {
                focus: Focus::Editor,
                ..
            }
        )
    {
        draw_dialog(frame, app, dialog, &mut targets);
    }
    targets
}
fn draw_general(frame: &mut Frame, app: &mut App, area: Rect, targets: &mut Targets) {
    let visible = usize::from(area.height);
    let stacked = area.width < 50;
    let style_y = if stacked { 5 } else { 2 };
    let help_y = if stacked { 9 } else { 6 };
    let help = "Compatibility uses plain text. Codicons requires a compatible Nerd Font.";
    let wrapped_lines = |text: &str| {
        text.split_whitespace()
            .fold((0_usize, 1_usize), |(column, lines), word| {
                let length = word.len();
                if column > 0 && column + 1 + length > usize::from(area.width) {
                    (length, lines + 1)
                } else {
                    (column + usize::from(column > 0) + length, lines)
                }
            })
            .1
    };
    let help_lines = wrapped_lines(help);
    let editor_y = help_y + help_lines + 3;
    let editor_open = matches!(
        app.dialog,
        Some(Dialog::Choice {
            focus: Focus::Editor,
            ..
        })
    );
    let editor_height = if editor_open { 6 } else { 3 };
    let size = (area.width, area.height);
    if size != app.general_size {
        app.general_size = size;
        app.general_top = 0;
    }
    let focused_control = match app.focus {
        Focus::Provider => Some((2, 3)),
        Focus::Style => Some((style_y, 3)),
        Focus::Editor => Some((editor_y, editor_height)),
        _ => None,
    };
    if let Some((y, height)) = focused_control {
        app.general_top = app
            .general_top
            .min(y)
            .max((y + height).saturating_sub(visible));
    }
    let lines = [
        (0, "Icon appearance"),
        (help_y, help),
        (editor_y - 2, "File editor"),
        (
            editor_y + editor_height + 1,
            "Environment uses $EDITOR, or vi if unset. Changes apply on the next file open. Editors are not installed automatically; unavailable commands are reported when opened.",
        ),
    ];
    for (y, text) in lines {
        if y >= app.general_top && y < app.general_top + visible {
            frame.render_widget(
                Paragraph::new(Span::styled(
                    text,
                    if y == 0 || y == editor_y - 2 {
                        Style::default()
                            .fg(Color::White)
                            .add_modifier(Modifier::BOLD)
                    } else {
                        Style::default().fg(Color::Gray)
                    },
                ))
                .wrap(Wrap { trim: false }),
                Rect::new(
                    area.x,
                    area.y + u16::try_from(y - app.general_top).unwrap_or(0),
                    area.width,
                    if y == 0 || y == editor_y - 2 {
                        1
                    } else {
                        u16::try_from((app.general_top + visible - y).min(wrapped_lines(text)))
                            .unwrap_or(1)
                    },
                ),
            );
            if (y == 0 || y == editor_y - 2) && y + 1 < app.general_top + visible {
                frame.render_widget(
                    Block::default().style(
                        Style::default()
                            .fg(Color::White)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Rect::new(
                        area.x,
                        area.y + u16::try_from(y + 1 - app.general_top).unwrap_or(0),
                        area.width,
                        1,
                    ),
                );
            }
        }
    }
    for (y, focus, label) in [
        (2, Focus::Provider, "Provider"),
        (style_y, Focus::Style, "Style"),
        (editor_y, Focus::Editor, "Editor"),
    ] {
        let height = if focus == Focus::Editor {
            editor_height
        } else {
            3
        };
        if y < app.general_top || y + height > app.general_top + visible {
            continue;
        }
        let value = if (focus == Focus::Editor && app.editor.is_none())
            || (focus != Focus::Editor && app.appearance.is_none())
        {
            "Unavailable".to_owned()
        } else {
            app.choices(focus)
                .into_iter()
                .find(|(_, v)| *v == app.current(focus))
                .map_or_else(|| "Unavailable".into(), |(s, _)| s)
        };
        let style_width = area.width.min(26);
        let (x, width) = if !stacked && focus == Focus::Provider {
            (area.x, area.width.saturating_sub(style_width))
        } else if !stacked && focus == Focus::Style {
            (area.right().saturating_sub(style_width), style_width)
        } else {
            (area.x, area.width)
        };
        let rect = Rect::new(
            x,
            area.y + u16::try_from(y - app.general_top).unwrap_or(0),
            width,
            u16::try_from(height).unwrap_or(3),
        );
        frame.render_widget(
            Block::bordered().style(Style::default().fg(Color::Gray).bg(Color::Black)),
            rect,
        );
        let inner = rect.inner(ratatui::layout::Margin {
            vertical: 1,
            horizontal: 1,
        });
        let label_width = u16::try_from(label.len() + 2)
            .unwrap_or(inner.width)
            .min(inner.width);
        let [label_rect, value_rect] =
            Layout::horizontal([Constraint::Length(label_width), Constraint::Min(0)]).areas(inner);
        frame.render_widget(
            Paragraph::new(Span::styled(
                format!("{label}:"),
                Style::default().fg(Color::White),
            ))
            .style(Style::default().fg(Color::Reset)),
            label_rect,
        );
        let focused = app.focus == focus;
        let value_style = Style::default().fg(Color::White);
        let value_style = if focused {
            value_style.add_modifier(Modifier::BOLD)
        } else {
            value_style
        };
        let text = ratatui::text::Line::from(vec![
            Span::styled(value.as_str(), value_style),
            Span::raw(
                " ".repeat(usize::from(value_rect.width).saturating_sub(value.chars().count() + 2)),
            ),
            Span::styled(
                "▼",
                Style::default()
                    .fg(if focused {
                        Color::Gray
                    } else {
                        Color::DarkGray
                    })
                    .add_modifier(if focused {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    }),
            ),
            Span::raw(" "),
        ]);
        frame.render_widget(
            Paragraph::new(text).style(Style::default().fg(Color::Reset).bg(Color::Black)),
            value_rect,
        );
        targets.0.push((rect, Hit::Focus(focus)));
        if focus == Focus::Editor
            && let Some(Dialog::Choice { index, .. }) = &app.dialog
        {
            for (i, (option, _)) in app.choices(focus).iter().enumerate() {
                let row = Rect::new(
                    rect.x + 1,
                    rect.y + 2 + u16::try_from(i).unwrap_or(0),
                    rect.width.saturating_sub(2),
                    1,
                );
                frame.render_widget(
                    Paragraph::new(format!("{} {option}", if i == *index { ">" } else { " " }))
                        .style(if i == *index {
                            Style::default().add_modifier(Modifier::REVERSED)
                        } else {
                            Style::default()
                        }),
                    row,
                );
                targets.0.push((row, Hit::Choice(i)));
            }
        }
    }
}
fn draw_plugins(frame: &mut Frame, app: &mut App, area: Rect, targets: &mut Targets) {
    let [intro, list, detail, actions] = Layout::vertical([
        Constraint::Length(if area.width < 46 { 3 } else { 2 }),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(3),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(ratatui::text::Text::from(vec![
            ratatui::text::Line::from(Span::styled(
                "Manage trusted local plugins.",
                Style::default().fg(Color::Gray),
            )),
            ratatui::text::Line::from(Span::styled(
                "i Install  e Enable  d Disable   u Uninstall",
                Style::default().fg(Color::Gray),
            )),
        ]))
        .wrap(Wrap { trim: false }),
        intro,
    );
    frame.render_widget(
        Block::bordered()
            .style(Style::default().fg(Color::White).bg(Color::Black))
            .border_style(Style::default().fg(Color::Gray)),
        list,
    );
    let inner = list.inner(ratatui::layout::Margin {
        vertical: 1,
        horizontal: 1,
    });
    let name_width = app
        .plugins
        .iter()
        .map(|plugin| plugin.name.chars().count())
        .max()
        .unwrap_or(6)
        .max(6)
        + 2;
    frame.render_widget(
        Paragraph::new(format!(" {:width$}State", "Plugin", width = name_width)).style(
            Style::default()
                .fg(Color::White)
                .bg(Color::DarkGray)
                .add_modifier(Modifier::BOLD),
        ),
        Rect::new(inner.x, inner.y, inner.width, inner.height.min(1)),
    );
    let visible = usize::from(list.height.saturating_sub(3));
    if app.row < app.list_top {
        app.list_top = app.row;
    }
    if app.row >= app.list_top.saturating_add(visible) {
        app.list_top = app.row.saturating_add(1).saturating_sub(visible);
    }
    for (index, plugin) in app
        .plugins
        .iter()
        .enumerate()
        .skip(app.list_top)
        .take(visible)
    {
        let rect = Rect::new(
            list.x.saturating_add(1),
            list.y
                .saturating_add(2)
                .saturating_add(u16::try_from(index - app.list_top).unwrap_or(0)),
            list.width.saturating_sub(2),
            1,
        );
        let state = if plugin.missing {
            "Missing"
        } else if plugin.enabled {
            "Enabled"
        } else if plugin.installed {
            "Installed"
        } else {
            "Available"
        };
        let style = if index == app.row {
            Style::default()
                .fg(Color::White)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::White).bg(Color::Black)
        };
        frame.render_widget(
            Paragraph::new(format!(
                " {:width$}{state}",
                plugin.name,
                width = name_width
            ))
            .style(style),
            rect,
        );
        targets.0.push((rect, Hit::Row(index)));
    }
    if let Some(plugin) = app.plugins.get(app.row) {
        frame.render_widget(
            Paragraph::new(Span::styled(
                format!(
                    "{}{}",
                    if app.protected.contains(&plugin.id) {
                        "Protected (running Desktop / Settings): "
                    } else {
                        ""
                    },
                    plugin.description
                ),
                Style::default().fg(Color::Gray),
            )),
            detail,
        );
    } else {
        frame.render_widget(
            Paragraph::new("No plugins found.").style(Style::default().fg(Color::Gray)),
            detail,
        );
    }
    let buttons = [
        (Focus::Install, "Install", Action::Install),
        (Focus::Enable, "Enable", Action::Enable),
        (Focus::Disable, "Disable", Action::Disable),
        (Focus::Uninstall, "Uninstall", Action::Uninstall),
    ];
    let width = if actions.width >= 44 {
        actions.width / 4
    } else {
        13
    };
    let count = usize::from((actions.width / width).max(1)).min(buttons.len());
    let focused = buttons
        .iter()
        .position(|(focus, _, _)| *focus == app.focus)
        .unwrap_or(0);
    let start = focused.saturating_add(1).saturating_sub(count);
    for (i, (focus, label, action)) in buttons.into_iter().skip(start).take(count).enumerate() {
        let x = actions
            .x
            .saturating_add(u16::try_from(i).unwrap_or(0).saturating_mul(width));
        let rect = Rect::new(
            x,
            actions.y,
            width.min(actions.right().saturating_sub(x)),
            actions.height,
        );
        let available = app.action_available(action);
        if available {
            control(
                frame,
                targets,
                rect,
                label,
                app.focus == focus,
                Hit::Focus(focus),
            );
        } else {
            frame.render_widget(
                Block::bordered().style(Style::default().fg(Color::DarkGray).bg(Color::Black)),
                rect,
            );
            frame.render_widget(
                Paragraph::new(ratatui::text::Line::from(vec![
                    Span::raw(" ".repeat(
                        usize::from(rect.width.saturating_sub(2)).saturating_sub(label.len() + 2)
                            / 2,
                    )),
                    Span::styled(
                        format!(" {label} "),
                        Style::default()
                            .fg(Color::DarkGray)
                            .add_modifier(Modifier::BOLD),
                    ),
                ]))
                .style(Style::default().fg(Color::Reset).bg(Color::Black)),
                rect.inner(ratatui::layout::Margin {
                    vertical: 1,
                    horizontal: 1,
                }),
            );
        }
    }
}
fn draw_dialog(frame: &mut Frame, app: &App, dialog: &Dialog, targets: &mut Targets) {
    let area = frame.area();
    let width = area.width.saturating_sub(2).min(70);
    let height = area.height.saturating_sub(2).min(14);
    let rect = Rect::new(
        area.x + area.width.saturating_sub(width) / 2,
        area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    frame.render_widget(Block::bordered().title("Settings"), rect);
    let inner = rect.inner(ratatui::layout::Margin {
        vertical: 1,
        horizontal: 1,
    });
    match dialog {
        Dialog::Choice { focus, index } => {
            for (i, (label, _)) in app
                .choices(*focus)
                .iter()
                .enumerate()
                .take(usize::from(inner.height))
            {
                let row = Rect::new(
                    inner.x,
                    inner.y + u16::try_from(i).unwrap_or(0),
                    inner.width,
                    1,
                );
                frame.render_widget(
                    Paragraph::new(format!("{} {label}", if i == *index { ">" } else { " " }))
                        .style(if i == *index {
                            Style::default().add_modifier(Modifier::REVERSED)
                        } else {
                            Style::default()
                        }),
                    row,
                );
                targets.0.push((row, Hit::Choice(i)));
            }
        }
        Dialog::Confirm { text, yes, .. } | Dialog::Consent { text, yes, .. } => {
            let [message, buttons] =
                Layout::vertical([Constraint::Min(1), Constraint::Length(3)]).areas(inner);
            frame.render_widget(
                Paragraph::new(text.as_str())
                    .wrap(Wrap { trim: false })
                    .scroll((app.feedback_top, 0)),
                message,
            );
            let [cancel, confirm] =
                Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
                    .areas(buttons);
            control(frame, targets, cancel, "Cancel", !yes, Hit::Cancel);
            control(frame, targets, confirm, "Confirm", *yes, Hit::Confirm);
        }
    }
}
