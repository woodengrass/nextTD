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
    TableScrollbar,
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
    Style::default().fg(Color::Reset).bg(Color::Reset)
}
fn selected(focus: bool) -> Style {
    if focus {
        style().reversed()
    } else {
        style().underlined()
    }
}
fn text(f: &mut Frame, r: Rect, s: &str, st: Style) {
    f.render_widget(Paragraph::new(s).style(st), r.intersection(f.area()))
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
pub fn preview_rows(ui: &Ui, pane: Rect) -> usize {
    let width = usize::from(pane.width.saturating_sub(2));
    let has_bar = ui.preview.as_ref().is_some_and(|preview| {
        width > 0 && pane.height > 5 && preview.lines.iter().any(|line| line.width() > width)
    });
    usize::from(pane.height.saturating_sub(5 + u16::from(has_bar)))
}

pub struct TableScrollbar {
    pub track: Rect,
    pub thumb: Rect,
    pub max_top: usize,
}

impl TableScrollbar {
    pub fn top_for_thumb_y(&self, y: u16) -> usize {
        let travel = self.track.height.saturating_sub(self.thumb.height);
        if travel == 0 {
            return 0;
        }
        let offset = usize::from(y.saturating_sub(self.track.y).min(travel));
        (self.max_top as u128 * offset as u128 / u128::from(travel)) as usize
    }
}

pub fn table_scrollbar(ui: &Ui, w: u16, h: u16) -> Option<TableScrollbar> {
    let table = panes(w, h, ui)[1];
    let rows = table.height.saturating_sub(3);
    let count = ui.listing.entries.len();
    if table.width < 2 || rows == 0 || count <= usize::from(rows) {
        return None;
    }
    let max_top = count - usize::from(rows);
    let thumb_height = ((u128::from(rows) * u128::from(rows) / count as u128) as u16)
        .clamp(1, rows.saturating_sub(1).max(1));
    let travel = rows - thumb_height;
    let offset = (ui.top.min(max_top) as u128 * u128::from(travel) / max_top as u128) as u16;
    let track = Rect::new(table.right() - 2, table.y + 3, 2, rows);
    let thumb = Rect::new(track.x, track.y + offset, track.width, thumb_height);
    Some(TableScrollbar {
        track,
        thumb,
        max_top,
    })
}
fn heading(f: &mut Frame, r: Rect, s: &str, focused: bool) {
    text(
        f,
        inset(line(r, 0), 1),
        s,
        if focused { selected(true) } else { style() }.bold(),
    );
    text(f, line(r, 1), &"─".repeat(r.width.into()), style().dim())
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
fn button(f: &mut Frame, r: Rect, s: &str, primary: bool, enabled: bool, focus: bool) {
    let st = if !enabled {
        style().dim()
    } else if primary {
        style().bold()
    } else {
        style()
    };
    f.render_widget(Block::default().style(st), r.intersection(f.area()));
    text(f, line(r, 0), &"▔".repeat(r.width.into()), st);
    text(f, line(r, 2), &"▁".repeat(r.width.into()), st);
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
        Dialog::Help => f.render_widget(Paragraph::new("File shortcuts\nArrows/j/k: select   Enter/l: open   Backspace/h: parent\nv: preview / list   b: folders   Tab: desktop focus   g: go to path\nn: new folder   f: new file   r: rename   e: edit\ny: copy   x: cut   p: paste   d: delete permanently\n.: hidden files   Ctrl+r: refresh\n\nEscape: close").wrap(Wrap{trim:false}).style(style()),body.intersection(f.area())),
        Dialog::Name { title, value, cursor, selected, button: b } => {
            text(f, line(body, 0), title, style());
            let border = if *b == 0 { style().bold() } else { style().dim() };
            for y in 0..input.height {
                let middle = match y { 0 => "▔", 2 => "▁", _ => " " };
                let edge = format!("▊{}▎", middle.repeat(input.width.saturating_sub(2).into()));
                text(f, line(input, y), &edge, border);
                if input.width > 0 { text(f, Rect::new(input.x, input.y + y, 1, 1), "▊", border.reversed()); }
            }
            let r = inset(line(input, 1), 3);
            let x = value.chars().take(*cursor).map(|c| c.width().unwrap_or(0)).sum::<usize>();
            let off = x.saturating_sub(r.width.saturating_sub(1).into());
            f.render_widget(Paragraph::new(value.as_str()).scroll((0, off.min(65535) as u16)).style(if *selected { style().reversed() } else { style() }), r.intersection(f.area()));
            let cursor = (r.x + (x - off) as u16, r.y);
            if *b == 0 && r.width > 0 && r.height > 0 && f.area().contains(cursor.into()) {
                f.buffer_mut()[cursor].set_style(if *selected { style().remove_modifier(Modifier::REVERSED) } else { style().reversed() });
            }
            button(f, cancel, "Cancel", false, true, *b == 1);
            button(f, submit, "Continue", true, true, *b == 2);
        }
        Dialog::Delete { name, button: b, ready } => {
            text(f, line(body, 0), "Delete permanently?", style());
            text(f, line(body, 1), &single_line(name), style());
            if *ready { text(f, line(body, 2), "Directories and their contents will be removed.", style()) }
            button(f, cancel, "Cancel", false, true, *b == 0);
            button(f, submit, "Delete", true, *ready, *b == 1);
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
            ui.focus == Focus::Folders,
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
            ui.focus == Focus::Table,
        );
        let scrollbar = table_scrollbar(ui, a.width, a.height);
        let mut rows = table;
        rows.width = rows
            .width
            .saturating_sub(if scrollbar.is_some() { 2 } else { 0 });
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
        if let Some(bar) = scrollbar {
            for y in bar.track.y..bar.track.bottom() {
                let thumb = y >= bar.thumb.y && y < bar.thumb.bottom();
                text(
                    f,
                    line(bar.track, y - bar.track.y),
                    if thumb { "██" } else { "░░" },
                    if thumb { style() } else { style().dim() },
                );
            }
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
                style().dim(),
            )
        }
    }
    if prev.width > 0 {
        heading(f, prev, "PREVIEW", ui.focus == Focus::Preview);
        let r = inset(prev, 1);
        if let Some(p) = &ui.preview {
            for (i, s) in p.meta.iter().enumerate() {
                text(
                    f,
                    line(r, 2 + i as u16),
                    &ellipsis(s, r.width),
                    style().dim(),
                )
            }
            let content_width = p.lines.iter().map(|s| s.width()).max().unwrap_or(0);
            let viewport = usize::from(r.width);
            let needs_bar = preview_rows(ui, prev) < usize::from(r.height.saturating_sub(5));
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
                        text(f, thumb, fractions[8 - start % 8], style().reversed());
                    } else if cell == end / 8 {
                        text(f, thumb, fractions[end % 8], style());
                    } else {
                        text(f, thumb, " ", style().reversed());
                    }
                }
            }
            for (i, s) in p
                .lines
                .iter()
                .skip(ui.preview_y)
                .take(preview_rows(ui, prev))
                .enumerate()
            {
                let mut parts = ratatui::text::Line::raw(s.as_str());
                if let Some((num, content)) = s.split_once(" │ ") {
                    parts = ratatui::text::Line::from(vec![
                        ratatui::text::Span::styled(format!("{num} │ "), style().dim()),
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
                style().dim(),
            )
        }
    }
    if let Some((s, sev)) = &ui.toast {
        let toast_style = match sev {
            Severity::Info => style(),
            Severity::Warning => style().bold(),
            Severity::Error => style().bold().underlined(),
        };
        let w = (s.width() + 4).min(usize::from(a.width)) as u16;
        let r = Rect::new(a.width - w, a.height.saturating_sub(4), w, 3);
        f.render_widget(Block::default().style(style()), r.intersection(a));
        text(f, inset(line(r, 1), 1), s, toast_style)
    }
    if let Some(d) = &ui.dialog {
        let buffer = f.buffer_mut();
        for y in a.y..a.bottom() {
            for x in a.x..a.right() {
                let cell = &mut buffer[(x, y)];
                cell.modifier.insert(Modifier::DIM);
            }
        }
        let (body, _, _, _) = dialogs(a, d);
        let area =
            Rect::new(body.x - 2, body.y - 1, body.width + 4, body.height + 2).intersection(a);
        f.render_widget(ratatui::widgets::Clear, area);
        f.render_widget(Block::default().style(style()), area);
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
    if table_scrollbar(ui, w, h).is_some_and(|bar| bar.track.contains(pt)) {
        return Hit::TableScrollbar;
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn ui() -> Ui {
        Ui {
            directory: PathBuf::from("/"),
            listing: Listing::default(),
            preview: None,
            row: 0,
            top: 0,
            neighbour: 0,
            neighbour_top: 0,
            preview_x: 0,
            preview_y: 0,
            focus: Focus::Table,
            compact_preview: false,
            dialog: None,
            toast: None,
            loading: false,
        }
    }

    #[test]
    fn focused_header_remains_visible_with_empty_or_loading_list() {
        let mut ui = ui();
        for width in [80, 100] {
            let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
            for loading in [false, true] {
                ui.loading = loading;
                ui.top = 100;
                ui.neighbour_top = 100;
                for focus in [Focus::Table, Focus::Folders, Focus::Preview] {
                    ui.focus = focus;
                    terminal.draw(|frame| draw(frame, &ui)).unwrap();
                    let buffer = terminal.backend().buffer();
                    let panes = panes(width, 24, &ui);
                    for (pane, pane_focus) in
                        panes
                            .into_iter()
                            .zip([Focus::Folders, Focus::Table, Focus::Preview])
                    {
                        if pane.width == 0 {
                            continue;
                        }
                        let cell = &buffer[(pane.x + 1, pane.y)];
                        assert_eq!(
                            cell.modifier.contains(Modifier::REVERSED),
                            focus == pane_focus
                        );
                        assert_eq!((cell.fg, cell.bg), (Color::Reset, Color::Reset));
                    }
                }
            }
        }
    }

    #[test]
    fn name_modal_survives_tiny_terminal() {
        let mut ui = ui();
        ui.dialog = Some(Dialog::Name {
            title: "Name".into(),
            value: "example".into(),
            cursor: 7,
            selected: false,
            button: 0,
        });
        for (w, h) in [(50, 4), (50, 3), (2, 1), (0, 0)] {
            let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
            terminal.draw(|frame| draw(frame, &ui)).unwrap();
        }
    }

    #[test]
    fn scrollbar_renders_and_captures_both_columns_at_every_track_row() {
        let mut ui = ui();
        ui.listing.entries = (0..30)
            .map(|i| crate::data::Entry {
                path: format!("file{i}").into(),
                ..Default::default()
            })
            .collect();
        ui.top = 10;
        let bar = table_scrollbar(&ui, 80, 15).unwrap();
        let mut terminal = Terminal::new(TestBackend::new(80, 15)).unwrap();
        terminal.draw(|frame| draw(frame, &ui)).unwrap();
        let buffer = terminal.backend().buffer();
        for y in bar.track.y..bar.track.bottom() {
            for x in bar.track.x..bar.track.right() {
                assert!(matches!(hit(&ui, 80, 15, x, y), Hit::TableScrollbar));
                let expected = if bar.thumb.contains((x, y).into()) {
                    "█"
                } else {
                    "░"
                };
                assert_eq!(buffer[(x, y)].symbol(), expected);
                assert_eq!(buffer[(x, y)].fg, Color::Reset);
                assert_eq!(buffer[(x, y)].bg, Color::Reset);
            }
        }
        assert!(matches!(
            hit(&ui, 80, 15, bar.track.x - 1, bar.track.y),
            Hit::Table(10)
        ));
        assert_eq!(bar.top_for_thumb_y(0), 0);
        assert_eq!(bar.top_for_thumb_y(u16::MAX), 20);
        ui.top = 20;
        let bottom = table_scrollbar(&ui, 80, 15).unwrap();
        assert_eq!(bottom.thumb.bottom(), bottom.track.bottom());
        ui.listing.entries.truncate(10);
        assert!(table_scrollbar(&ui, 80, 15).is_none());
    }

    #[test]
    fn wide_preview_last_line_fits_above_horizontal_scrollbar() {
        let mut ui = ui();
        ui.preview = Some(Preview {
            meta: vec![],
            lines: (0..12)
                .map(|i| format!("{i:02} {}", "x".repeat(100)))
                .collect(),
        });
        let pane = panes(80, 15, &ui)[2];
        assert_eq!(preview_rows(&ui, pane), 7);
        ui.preview_y = 12 - preview_rows(&ui, pane);
        let mut terminal = Terminal::new(TestBackend::new(80, 15)).unwrap();
        terminal.draw(|frame| draw(frame, &ui)).unwrap();
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(pane.x + 1, pane.y + 11)].symbol(), "1");
        assert_eq!(buffer[(pane.x + 2, pane.y + 11)].symbol(), "1");
    }

    #[test]
    fn terminal_defaults_and_semantic_selection_are_used_for_table_and_preview() {
        let mut ui = ui();
        ui.listing.entries.push(crate::data::Entry {
            path: "file".into(),
            ..Default::default()
        });
        ui.preview = Some(Preview {
            meta: vec!["metadata".into()],
            lines: vec!["content".into()],
        });
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|frame| draw(frame, &ui)).unwrap();
        let buffer = terminal.backend().buffer();
        let [_, table, preview] = panes(80, 24, &ui);
        for cell in &buffer.content {
            assert_eq!((cell.fg, cell.bg), (Color::Reset, Color::Reset));
        }
        assert!(
            buffer[(table.x + 1, table.y + 3)]
                .modifier
                .contains(Modifier::REVERSED)
        );
        assert!(
            buffer[(preview.x + 1, preview.y + 2)]
                .modifier
                .contains(Modifier::DIM)
        );
        assert!(
            !buffer[(preview.x + 1, preview.y + 5)]
                .modifier
                .contains(Modifier::REVERSED)
        );
    }

    #[test]
    fn modal_uses_default_colors_and_semantic_focus() {
        let mut ui = ui();
        ui.dialog = Some(Dialog::Name {
            title: "Name".into(),
            value: "abc".into(),
            cursor: 1,
            selected: false,
            button: 0,
        });
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|frame| draw(frame, &ui)).unwrap();
        let buffer = terminal.backend().buffer();
        let (body, input, _, submit) =
            dialogs(Rect::new(0, 0, 80, 24), ui.dialog.as_ref().unwrap());
        for cell in &buffer.content {
            assert_eq!((cell.fg, cell.bg), (Color::Reset, Color::Reset));
        }
        assert!(buffer[(0, 0)].modifier.contains(Modifier::DIM));
        assert!(!buffer[(body.x, body.y)].modifier.contains(Modifier::DIM));
        assert!(
            buffer[(input.x + 4, input.y + 1)]
                .modifier
                .contains(Modifier::REVERSED)
        );
        assert!(
            buffer[(submit.x, submit.y)]
                .modifier
                .contains(Modifier::BOLD)
        );
    }
}
