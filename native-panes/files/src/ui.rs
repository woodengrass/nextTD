use crate::data::{Listing, Preview, single_line};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    widgets::{Block, Paragraph, Wrap},
};
use std::path::PathBuf;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Table,
    Folders,
    Preview,
}
#[derive(Clone, Copy)]
pub enum Severity {
    Info,
    Warning,
    Error,
}
pub enum Dialog {
    Help,
    Name {
        title: String,
        value: String,
        cursor: usize,
        selected: bool,
        button: usize,
    },
    Delete {
        name: String,
        button: usize,
        ready: bool,
    },
}
#[derive(Clone, Copy)]
pub enum Hit {
    None,
    Table(usize),
    Folder(usize),
    Preview,
    Input,
    Cancel,
    Submit,
}
pub struct Ui {
    pub directory: PathBuf,
    pub listing: Listing,
    pub preview: Option<Preview>,
    pub row: usize,
    pub top: usize,
    pub neighbour: usize,
    pub neighbour_top: usize,
    pub preview_x: usize,
    pub preview_y: usize,
    pub focus: Focus,
    pub compact_preview: bool,
    pub dialog: Option<Dialog>,
    pub toast: Option<(String, Severity)>,
    pub loading: bool,
}
fn style() -> Style {
    Style::default().fg(Color::White).bg(Color::Black)
}
fn selected(focus: bool) -> Style {
    style().bg(if focus { Color::Cyan } else { Color::DarkGray })
}
fn text(f: &mut Frame, r: Rect, s: &str, st: Style) {
    f.render_widget(Paragraph::new(s).style(st), r)
}
fn line(r: Rect, n: u16) -> Rect {
    Rect::new(r.x, r.y + n, r.width, u16::from(n < r.height))
}
fn inset(r: Rect, n: u16) -> Rect {
    Rect::new(r.x + n, r.y, r.width.saturating_sub(n * 2), r.height)
}
fn ellipsis(s: &str, w: u16) -> String {
    if s.width() <= usize::from(w) {
        return s.into();
    }
    let mut v = String::new();
    let mut len = 0;
    for c in s.chars() {
        len += c.width().unwrap_or(0);
        if len >= usize::from(w) {
            break;
        }
        v.push(c)
    }
    v.push('…');
    v
}
pub fn panes(width: u16, height: u16, ui: &Ui) -> [Rect; 3] {
    let w = width.saturating_sub(2);
    let folders = if w >= 96 { 20 } else { 0 };
    let left = folders + u16::from(folders > 0);
    let remaining = w.saturating_sub(left);
    let preview = if w < 58 {
        if ui.compact_preview { w } else { 0 }
    } else {
        (u32::from(remaining.saturating_sub(1)) * 40 / 100) as u16
    };
    let table = remaining.saturating_sub(preview + u16::from(w >= 58));
    let h = height.saturating_sub(2);
    [
        Rect::new(1, 2, folders, h),
        Rect::new(1 + left, 2, table, h),
        Rect::new(1 + w - preview, 2, preview, h),
    ]
}
fn heading(f: &mut Frame, r: Rect, s: &str) {
    text(f, inset(line(r, 0), 1), s, style().bold());
    text(
        f,
        line(r, 1),
        &"─".repeat(r.width.into()),
        style().fg(Color::Gray),
    )
}
use ratatui::style::Stylize;
fn table_row(f: &mut Frame, r: Rect, cols: [&str; 3], st: Style) {
    f.render_widget(Block::default().style(st), r);
    let avail = r.width.saturating_sub(6);
    let widths = if avail >= 45 {
        [avail - 25, 9, 16]
    } else if avail >= 27 {
        [avail - 9, 9, 0]
    } else {
        [avail, 0, 0]
    };
    let mut x = r.x;
    for (s, w) in cols.into_iter().zip(widths) {
        if w > 0 {
            text(f, Rect::new(x + 1, r.y, w, r.height), &ellipsis(s, w), st)
        }
        x += w + 2
    }
}
fn dialogs(area: Rect, d: &Dialog) -> (Rect, Rect, Rect, Rect) {
    let (w, h) = match d {
        Dialog::Help => (68, 11),
        Dialog::Name { .. } => (60, 11),
        Dialog::Delete { .. } => (64, 9),
    };
    let w = w.min((u32::from(area.width) * 9 / 10) as u16);
    let h = h.min(area.height);
    let r = Rect::new((area.width - w) / 2, (area.height - h) / 2, w, h);
    let body = Rect::new(r.x + 2, r.y + 1, w.saturating_sub(4), h.saturating_sub(2));
    let input = Rect::new(body.x, body.y + 2, body.width, 3);
    let btn = 16.min(body.width / 2);
    let submit = Rect::new(
        body.right() - btn,
        body.y
            + if matches!(d, Dialog::Name { .. }) {
                6
            } else {
                4
            },
        btn,
        3,
    );
    let cancel = Rect::new(submit.x - btn, submit.y, btn, 3);
    (body, input, cancel, submit)
}
fn button(f: &mut Frame, r: Rect, s: &str, color: Color, focus: bool) {
    let st = style().bg(color);
    f.render_widget(Block::default().style(st), r);
    text(
        f,
        line(r, 0),
        &"▔".repeat(r.width.into()),
        st.fg(if color == Color::Cyan {
            Color::LightCyan
        } else {
            Color::DarkGray
        }),
    );
    text(
        f,
        line(r, 2),
        &"▁".repeat(r.width.into()),
        st.fg(if color == Color::Cyan {
            Color::Blue
        } else {
            Color::Black
        }),
    );
    let label = format!(" {s} ");
    let x = r.x + r.width.saturating_sub(label.len() as u16) / 2;
    text(
        f,
        Rect::new(x, r.y + 1, label.len() as u16, 1),
        &label,
        if focus {
            st.bold().reversed()
        } else {
            st.bold()
        },
    )
}
fn modal(f: &mut Frame, d: &Dialog) {
    let (body, input, cancel, submit) = dialogs(f.area(), d);
    match d {
        Dialog::Help => f.render_widget(Paragraph::new("File shortcuts\nArrows/j/k: select   Enter/l: open   Backspace/h: parent\nv: preview / list   b: folders   Tab: desktop focus   g: go to path\nn: new folder   f: new file   r: rename   e: edit\ny: copy   x: cut   p: paste   d: delete permanently\n.: hidden files   Ctrl+r: refresh\n\nEscape: close").wrap(Wrap{trim:false}).style(style()),body),
        Dialog::Name { title, value, cursor, selected, button: b } => {
            text(f, line(body, 0), title, style());
            let border = style().fg(if *b == 0 { Color::Cyan } else { Color::DarkGray });
            for y in 0..input.height {
                let middle = match y { 0 => "▔", 2 => "▁", _ => " " };
                let edge = format!("▊{}▎", middle.repeat(input.width.saturating_sub(2).into()));
                text(f, line(input, y), &edge, border);
                if input.width > 0 { text(f, Rect::new(input.x, input.y + y, 1, 1), "▊", border.reversed()); }
            }
            let r = inset(line(input, 1), 3);
            let x = value.chars().take(*cursor).map(|c| c.width().unwrap_or(0)).sum::<usize>();
            let off = x.saturating_sub(r.width.saturating_sub(1).into());
            f.render_widget(Paragraph::new(value.as_str()).scroll((0, off.min(65535) as u16)).style(if *selected { style().reversed() } else { style() }), r);
            if *b == 0 && r.width > 0 {
                f.buffer_mut()[(r.x + (x - off) as u16, r.y)].set_style(Style::default().fg(Color::Black).bg(Color::White).remove_modifier(Modifier::REVERSED));
            }
            button(f, cancel, "Cancel", Color::Black, *b == 1);
            button(f, submit, "Continue", Color::Cyan, *b == 2);
        }
        Dialog::Delete { name, button: b, ready } => {
            text(f, line(body, 0), "Delete permanently?", style());
            text(f, line(body, 1), &single_line(name), style());
            if *ready { text(f, line(body, 2), "Directories and their contents will be removed.", style()) }
            button(f, cancel, "Cancel", Color::Black, *b == 0);
            button(f, submit, "Delete", if *ready { Color::Red } else { Color::DarkGray }, *b == 1);
        }
    }
}
pub fn draw(f: &mut Frame, ui: &Ui) {
    let a = f.area();
    f.render_widget(Block::default().style(style()), a);
    text(
        f,
        inset(line(a, 1), 1),
        &ellipsis(
            &format!("Files  /  {}", single_line(&ui.directory.to_string_lossy())),
            a.width.saturating_sub(2),
        ),
        style().bold(),
    );
    let [folders, table, prev] = panes(a.width, a.height, ui);
    if folders.width > 0 {
        heading(
            f,
            folders,
            if ui.listing.neighbour_error.is_empty() {
                "FOLDERS"
            } else {
                "FOLDERS (unavailable)"
            },
        );
        let parent = usize::from(ui.directory.parent().is_some());
        for row in 0..folders.height.saturating_sub(2) {
            let i = ui.neighbour_top + usize::from(row);
            let label = if parent == 1 && i == 0 && !ui.loading {
                "  ..  Parent".into()
            } else if let Some(p) = i
                .checked_sub(parent)
                .and_then(|i| ui.listing.neighbours.get(i))
            {
                format!(
                    "{} {}",
                    if p == &ui.directory { ">" } else { " " },
                    single_line(&p.file_name().unwrap_or(p.as_os_str()).to_string_lossy())
                )
            } else {
                break;
            };
            text(
                f,
                line(folders, row + 2),
                &ellipsis(&label, folders.width),
                if ui.neighbour == i {
                    selected(ui.focus == Focus::Folders)
                } else {
                    style()
                },
            )
        }
    }
    let err = &ui.listing.error;
    let empty = ui.listing.entries.is_empty();
    if table.width > 0 {
        heading(
            f,
            table,
            &if !ui.loading && err.is_empty() {
                format!("FILES  ·  {}", ui.listing.entries.len())
            } else {
                "FILES".into()
            },
        );
        let scroll = ui.listing.entries.len() > usize::from(table.height.saturating_sub(3));
        let mut rows = table;
        rows.width = rows.width.saturating_sub(if scroll { 2 } else { 0 });
        table_row(
            f,
            line(rows, 2),
            ["Name", "Size", "Modified"],
            selected(false).bold(),
        );
        for (n, e) in ui
            .listing
            .entries
            .iter()
            .skip(ui.top)
            .take(table.height.saturating_sub(3).into())
            .enumerate()
        {
            let name = single_line(&e.path.file_name().unwrap_or_default().to_string_lossy())
                + if e.directory { "/" } else { "" };
            table_row(
                f,
                line(rows, 3 + n as u16),
                [&name, &e.size, &e.modified],
                if n + ui.top == ui.row {
                    selected(ui.focus == Focus::Table).bold()
                } else {
                    style()
                },
            )
        }
        if empty {
            let status = if ui.loading {
                "Loading folder…"
            } else if !err.is_empty() {
                err
            } else {
                "Empty folder — n: folder, f: file"
            };
            text(
                f,
                line(table, table.height.saturating_sub(1)),
                status,
                style().fg(Color::Gray),
            )
        }
    }
    if prev.width > 0 {
        heading(f, prev, "PREVIEW");
        let r = inset(prev, 1);
        if let Some(p) = &ui.preview {
            for (i, s) in p.meta.iter().enumerate() {
                text(
                    f,
                    line(r, 2 + i as u16),
                    &ellipsis(s, r.width),
                    style().fg(Color::Gray),
                )
            }
            let content_width = p.lines.iter().map(|s| s.width()).max().unwrap_or(0);
            let viewport = usize::from(r.width);
            let needs_bar = content_width > viewport && viewport > 0 && r.height > 5;
            if needs_bar {
                let thumb = (viewport * viewport).max(content_width);
                let start = ((viewport * content_width - thumb)
                    * ui.preview_x.min(content_width - viewport)
                    * 8)
                    / (content_width * (content_width - viewport));
                let end = start + (thumb * 8).div_ceil(content_width);
                let row = line(r, r.height - 1);
                let fractions = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉"];
                for cell in start / 8..end.div_ceil(8).min(viewport) {
                    let thumb = Rect::new(row.x + cell as u16, row.y, 1, 1);
                    if cell == start / 8 && start % 8 != 0 {
                        text(
                            f,
                            thumb,
                            fractions[8 - start % 8],
                            style().fg(Color::DarkGray).reversed(),
                        );
                    } else if cell == end / 8 {
                        text(f, thumb, fractions[end % 8], style().fg(Color::DarkGray));
                    } else {
                        text(f, thumb, " ", style().fg(Color::DarkGray).reversed());
                    }
                }
            }
            for (i, s) in p
                .lines
                .iter()
                .skip(ui.preview_y)
                .take(r.height.saturating_sub(5 + u16::from(needs_bar)).into())
                .enumerate()
            {
                let mut parts = ratatui::text::Line::raw(s.as_str());
                if let Some((num, content)) = s.split_once(" │ ") {
                    parts = ratatui::text::Line::from(vec![
                        ratatui::text::Span::styled(format!("{num} │ "), style().fg(Color::Gray)),
                        content.into(),
                    ])
                }
                f.render_widget(
                    Paragraph::new(parts)
                        .scroll((0, ui.preview_x.min(65535) as u16))
                        .style(style()),
                    line(r, 5 + i as u16),
                )
            }
        } else {
            let loading_preview;
            text(
                f,
                line(r, 2),
                if ui.loading {
                    "Loading folder…"
                } else if !err.is_empty() {
                    err
                } else if empty {
                    "This folder is empty. Press n or f to create an item."
                } else {
                    let name = ui
                        .listing
                        .entries
                        .get(ui.row)
                        .and_then(|e| e.path.file_name())
                        .unwrap_or_default();
                    loading_preview =
                        format!("Loading preview: {}", single_line(&name.to_string_lossy()));
                    &loading_preview
                },
                style().fg(Color::Gray),
            )
        }
    }
    if let Some((s, sev)) = &ui.toast {
        let color = match sev {
            Severity::Info => Color::Cyan,
            Severity::Warning => Color::Yellow,
            Severity::Error => Color::Red,
        };
        let w = (s.width() + 4).min(usize::from(a.width)) as u16;
        let r = Rect::new(a.width - w, a.height.saturating_sub(4), w, 3);
        f.render_widget(Block::default().style(style()), r);
        text(f, inset(line(r, 1), 1), s, style().fg(color))
    }
    if let Some(d) = &ui.dialog {
        let buffer = f.buffer_mut();
        for y in a.y..a.bottom() {
            for x in a.x..a.right() {
                let cell = &mut buffer[(x, y)];
                cell.fg = match cell.fg {
                    Color::White | Color::Gray => Color::DarkGray,
                    Color::DarkGray => Color::Black,
                    color => color,
                };
                cell.bg = match cell.bg {
                    Color::Cyan => Color::DarkGray,
                    Color::DarkGray => Color::Black,
                    color => color,
                };
            }
        }
        let (body, _, _, _) = dialogs(a, d);
        let area = Rect::new(body.x - 2, body.y - 1, body.width + 4, body.height + 2);
        f.render_widget(ratatui::widgets::Clear, area);
        f.render_widget(
            Block::default().style(Style::default().bg(Color::Black)),
            area,
        );
        modal(f, d);
    }
}
pub fn hit(ui: &Ui, w: u16, h: u16, x: u16, y: u16) -> Hit {
    let pt = (x, y).into();
    if let Some(d) = &ui.dialog {
        let (_, input, cancel, submit) = dialogs(Rect::new(0, 0, w, h), d);
        return if matches!(d, Dialog::Help) {
            Hit::None
        } else if cancel.contains(pt) {
            Hit::Cancel
        } else if submit.contains(pt) {
            Hit::Submit
        } else if matches!(d, Dialog::Name { .. }) && input.contains(pt) {
            Hit::Input
        } else {
            Hit::None
        };
    }
    let [folder, table, prev] = panes(w, h, ui);
    if table.contains(pt) && y >= table.y + 3 {
        let idx = ui.top + usize::from(y - table.y - 3);
        if idx < ui.listing.entries.len() {
            return Hit::Table(idx);
        }
    }
    if folder.contains(pt) && y >= folder.y + 2 {
        let idx = ui.neighbour_top + usize::from(y - folder.y - 2);
        if idx < ui.listing.neighbours.len() + usize::from(ui.directory.parent().is_some()) {
            return Hit::Folder(idx);
        }
    }
    if prev.contains(pt) {
        Hit::Preview
    } else {
        Hit::None
    }
}
