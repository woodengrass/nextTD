use crate::ui::{self, Dialog, Focus, Ui};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier},
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

fn paint_rect(f: &mut Frame, rect: Rect, fg: Color, bg: Color, modifiers: Modifier) {
    let rect = rect.intersection(f.area());
    for y in rect.y..rect.bottom() {
        for x in rect.x..rect.right() {
            let cell = &mut f.buffer_mut()[(x, y)];
            cell.fg = fg;
            cell.bg = bg;
            cell.modifier = modifiers;
        }
    }
}

fn recolor_text(f: &mut Frame, rect: Rect, fg: Color, bg: Color) {
    let rect = rect.intersection(f.area());
    for y in rect.y..rect.bottom() {
        let first = (rect.x..rect.right()).find(|&x| f.buffer_mut()[(x, y)].symbol() != " ");
        let last = (rect.x..rect.right())
            .rev()
            .find(|&x| f.buffer_mut()[(x, y)].symbol() != " ");
        for x in rect.x..rect.right() {
            let cell = &mut f.buffer_mut()[(x, y)];
            cell.fg = if first.is_none_or(|first| x < first) || last.is_none_or(|last| x > last) {
                Color::Reset
            } else {
                fg
            };
            cell.bg = bg;
            cell.modifier
                .remove(Modifier::DIM | Modifier::REVERSED | Modifier::UNDERLINED);
            if first.is_none_or(|first| x < first) || last.is_none_or(|last| x > last) {
                cell.modifier.remove(Modifier::BOLD);
            }
        }
    }
}

pub fn paint(f: &mut Frame, ui: &Ui) {
    let area = f.area();
    let [folders, table, preview] = ui::panes(area.width, area.height, ui);
    let scrollbar = if preview.width > 2
        && preview.height > 5
        && ui::preview_rows(ui, preview) < usize::from(preview.height.saturating_sub(5))
    {
        let y = preview.bottom() - 1;
        (preview.x + 1..preview.right() - 1)
            .filter(|&x| f.buffer_mut()[(x, y)].modifier.contains(Modifier::REVERSED))
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    recolor_text(f, area, Color::White, Color::Black);
    for pane in [folders, preview] {
        recolor_text(f, pane, Color::White, Color::Black);
    }
    if folders.width > 0 {
        paint_rect(
            f,
            Rect::new(
                folders.x,
                folders.y.saturating_add(2),
                folders.width,
                folders.height.saturating_sub(2),
            ),
            Color::White,
            Color::Black,
            Modifier::empty(),
        );
    }
    if table.width > 0 {
        let body = Rect::new(
            table.x,
            table.y.saturating_add(2),
            table.width,
            table.height.saturating_sub(2),
        );
        paint_rect(f, body, Color::White, Color::Black, Modifier::empty());
        let header = Rect::new(table.x, table.y.saturating_add(2), table.width, 1);
        paint_rect(f, header, Color::White, Color::DarkGray, Modifier::BOLD);
        if !ui.listing.entries.is_empty() && ui.row >= ui.top {
            let y = table
                .y
                .saturating_add(3)
                .saturating_add(u16::try_from(ui.row - ui.top).unwrap_or(u16::MAX));
            if y < table.bottom() {
                let (fg, bg, modifier) = if ui.focus == Focus::Table {
                    (Color::White, Color::Cyan, Modifier::BOLD)
                } else {
                    (Color::White, Color::DarkGray, Modifier::empty())
                };
                paint_rect(f, Rect::new(table.x, y, table.width, 1), fg, bg, modifier);
            }
        }
    }
    if folders.width > 0 && ui.neighbour >= ui.neighbour_top {
        let y = folders
            .y
            .saturating_add(2)
            .saturating_add(u16::try_from(ui.neighbour - ui.neighbour_top).unwrap_or(u16::MAX));
        if y < folders.bottom()
            && ui.neighbour
                < ui.listing.neighbours.len() + usize::from(ui.directory.parent().is_some())
        {
            paint_rect(
                f,
                Rect::new(folders.x, y, folders.width, 1),
                Color::White,
                if ui.focus == Focus::Folders {
                    Color::Cyan
                } else {
                    Color::DarkGray
                },
                Modifier::empty(),
            );
        }
    }
    for pane in [folders, table, preview] {
        if pane.width > 0 && pane.height > 1 {
            recolor_text(
                f,
                Rect::new(pane.x, pane.y, pane.width, 1),
                Color::White,
                Color::Black,
            );
            paint_rect(
                f,
                Rect::new(pane.x, pane.y + 1, pane.width, 1),
                Color::Gray,
                Color::Black,
                Modifier::empty(),
            );
        }
    }
    for y in area.y.saturating_add(2)..area.bottom() {
        for x in area.x..area.right() {
            if ![folders, table, preview]
                .into_iter()
                .any(|pane| pane.contains((x, y).into()))
            {
                let cell = &mut f.buffer_mut()[(x, y)];
                cell.fg = Color::Reset;
                cell.modifier = Modifier::empty();
            }
        }
    }
    recolor_text(
        f,
        Rect::new(
            area.x,
            area.y.saturating_add(1),
            area.width,
            u16::from(area.height > 1),
        ),
        Color::White,
        Color::Black,
    );
    if preview.width > 0 {
        let meta = Rect::new(
            preview.x + 1,
            preview.y + 2,
            preview.width.saturating_sub(2),
            3.min(preview.height.saturating_sub(2)),
        );
        recolor_text(f, meta, Color::Gray, Color::Black);
        for y in preview.y.saturating_add(5)..preview.bottom() {
            let mut in_prefix = true;
            let has_prefix = (preview.x.saturating_add(1)..preview.right().saturating_sub(1))
                .any(|x| f.buffer_mut()[(x, y)].symbol() == "│");
            for x in preview.x.saturating_add(1)..preview.right().saturating_sub(1) {
                let after_prefix =
                    has_prefix && x > preview.x + 1 && f.buffer_mut()[(x - 1, y)].symbol() == "│";
                let cell = &mut f.buffer_mut()[(x, y)];
                if cell.symbol() == "│" {
                    cell.fg = Color::Gray;
                    in_prefix = false;
                } else if has_prefix && in_prefix || after_prefix {
                    cell.fg = Color::Gray;
                } else if !cell.symbol().trim().is_empty() {
                    cell.fg = Color::White;
                }
            }
        }
    }
    for x in scrollbar {
        f.buffer_mut()[(x, preview.bottom() - 1)]
            .modifier
            .insert(Modifier::REVERSED);
    }
}

pub fn dim(cell: &mut ratatui::buffer::Cell, table_header: bool) {
    cell.fg = if cell.fg == Color::Reset {
        Color::White
    } else {
        Color::DarkGray
    };
    cell.bg = match cell.bg {
        Color::Cyan => Color::DarkGray,
        Color::DarkGray if table_header => Color::Black,
        Color::DarkGray => Color::Black,
        color => color,
    };
    cell.modifier
        .remove(Modifier::DIM | Modifier::REVERSED | Modifier::UNDERLINED);
}

pub fn paint_modal(f: &mut Frame, dialog: &Dialog) {
    let area = f.area();
    let (body, input, cancel, submit) = ui::dialogs(area, dialog);
    let modal = Rect::new(
        body.x.saturating_sub(2),
        body.y.saturating_sub(1),
        body.width.saturating_add(4),
        body.height.saturating_add(2),
    )
    .intersection(area);
    recolor_text(f, modal, Color::White, Color::Black);
    if let Dialog::Name {
        selected,
        cursor,
        value,
        button,
        ..
    } = dialog
    {
        for y in input.y..input.bottom().min(area.bottom()) {
            for x in input.x..input.right().min(area.right()) {
                let cell = &mut f.buffer_mut()[(x, y)];
                cell.bg = Color::Black;
                cell.fg = if x == input.x || x + 1 == input.right() || y != input.y + 1 {
                    if *button == 0 {
                        Color::Cyan
                    } else {
                        Color::Gray
                    }
                } else if x < input.x.saturating_add(3) || x >= input.right().saturating_sub(3) {
                    Color::Reset
                } else {
                    Color::White
                };
                cell.modifier = if x == input.x {
                    Modifier::REVERSED
                } else {
                    Modifier::empty()
                };
            }
        }
        let width = usize::from(input.width.saturating_sub(6));
        let cursor_width: usize = value
            .chars()
            .take(*cursor)
            .map(|c| c.width().unwrap_or(0))
            .sum();
        let offset = cursor_width.saturating_sub(width.saturating_sub(1));
        let start = input.x.saturating_add(3);
        if *selected && input.y.saturating_add(1) < area.bottom() {
            let visible = value.width().saturating_sub(offset).min(width);
            for x in start
                ..start
                    .saturating_add(u16::try_from(visible).unwrap_or(u16::MAX))
                    .min(area.right())
            {
                let cell = &mut f.buffer_mut()[(x, input.y + 1)];
                cell.fg = Color::White;
                cell.bg = Color::DarkGray;
            }
        }
        let x = start.saturating_add(u16::try_from(cursor_width - offset).unwrap_or(u16::MAX));
        if *button == 0 && width > 0 && x < area.right() && input.y + 1 < area.bottom() {
            let cell = &mut f.buffer_mut()[(x, input.y + 1)];
            cell.fg = Color::Black;
            cell.bg = Color::White;
        }
    }
    if !matches!(dialog, Dialog::Help) {
        let focus = match dialog {
            Dialog::Name { button, .. } | Dialog::Delete { button, .. } => *button,
            Dialog::Help => 0,
        };
        let cancel_focus = matches!(dialog, Dialog::Delete { .. }) && focus == 0
            || matches!(dialog, Dialog::Name { .. }) && focus == 1;
        let submit_focus = matches!(dialog, Dialog::Delete { .. }) && focus == 1
            || matches!(dialog, Dialog::Name { .. }) && focus == 2;
        for (rect, primary, selected) in
            [(cancel, false, cancel_focus), (submit, true, submit_focus)]
        {
            let destructive = matches!(dialog, Dialog::Delete { .. });
            let bg = if primary {
                if destructive {
                    Color::LightRed
                } else {
                    Color::Cyan
                }
            } else {
                Color::Black
            };
            let label_len = if primary {
                if destructive { 8 } else { 10 }
            } else {
                8
            };
            let label_start = rect.x + rect.width.saturating_sub(label_len) / 2;
            for y in rect.y..rect.bottom().min(area.bottom()) {
                for x in rect.x..rect.right().min(area.right()) {
                    let cell = &mut f.buffer_mut()[(x, y)];
                    cell.bg = bg;
                    cell.fg = if y == rect.y {
                        if primary {
                            if destructive {
                                Color::LightRed
                            } else {
                                Color::LightCyan
                            }
                        } else {
                            Color::DarkGray
                        }
                    } else if y + 1 == rect.bottom() {
                        if primary {
                            if destructive { Color::Red } else { Color::Blue }
                        } else {
                            Color::Black
                        }
                    } else if x >= label_start && x < label_start.saturating_add(label_len) {
                        Color::White
                    } else {
                        Color::Reset
                    };
                    cell.modifier = if y == rect.y + 1
                        && x >= label_start
                        && x < label_start.saturating_add(label_len)
                    {
                        Modifier::BOLD
                            | if selected {
                                Modifier::REVERSED
                            } else {
                                Modifier::empty()
                            }
                    } else {
                        Modifier::empty()
                    };
                    if primary && matches!(dialog, Dialog::Delete { ready: false, .. }) {
                        cell.fg = Color::White;
                        cell.bg = Color::Black;
                        cell.modifier.insert(Modifier::DIM);
                    }
                }
            }
        }
    }
}
