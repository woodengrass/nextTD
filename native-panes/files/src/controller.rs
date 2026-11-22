use crate::{
    actions,
    data::{self, Listing, Preview},
    ui::{self, Dialog, Focus, Hit, Severity, Ui},
};
use crossterm::event::{
    Event, KeyCode as K, KeyEvent, KeyEventKind, KeyModifiers as M, MouseButton,
    MouseEventKind as Mouse,
};
use std::{
    path::PathBuf,
    process::{Command, ExitStatus},
    sync::mpsc::{self, Receiver},
    time::{Duration, Instant},
};
pub enum Effect {
    None,
    Quit,
    Edit(Box<Command>, PathBuf),
}
enum Name {
    Folder,
    File,
    Rename(PathBuf),
    Go,
}
enum Job {
    List(PathBuf, bool, Option<PathBuf>),
    Preview(PathBuf),
}
enum Reply {
    List(Listing, Option<PathBuf>),
    Preview(Preview),
}
pub struct Controller {
    pub ui: Ui,
    hidden: bool,
    clip: Option<(PathBuf, bool)>,
    name: Option<Name>,
    delete: Option<PathBuf>,
    revision: u64,
    worker: Option<(u64, Receiver<Reply>)>,
    queued: Option<Job>,
    expires: Option<Instant>,
    width: u16,
    height: u16,
}
impl Controller {
    pub fn new(dir: PathBuf) -> Self {
        let mut c = Self {
            ui: Ui {
                directory: dir,
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
                loading: true,
            },
            hidden: false,
            clip: None,
            name: None,
            delete: None,
            revision: 0,
            worker: None,
            queued: None,
            expires: None,
            width: 80,
            height: 24,
        };
        c.reload(None);
        c
    }
    fn schedule(&mut self, j: Job) {
        self.revision += 1;
        if self.worker.is_some() {
            self.queued = Some(j)
        } else {
            self.spawn(j)
        }
    }
    fn spawn(&mut self, j: Job) {
        let (tx, rx) = mpsc::channel();
        self.worker = Some((self.revision, rx));
        std::thread::spawn(move || {
            let reply = match j {
                Job::List(p, h, s) => Reply::List(data::list(&p, h), s),
                Job::Preview(p) => Reply::Preview(data::preview(&p)),
            };
            let _ = tx.send(reply);
        });
    }
    pub fn poll(&mut self) {
        if self.expires.is_some_and(|t| Instant::now() >= t) {
            self.expires = None;
            self.ui.toast = None
        }
        let Some((rev, rx)) = &self.worker else {
            return;
        };
        let rev = *rev;
        let reply = match rx.try_recv() {
            Ok(r) => Some(r),
            Err(mpsc::TryRecvError::Empty) => return,
            Err(_) => None,
        };
        self.worker = None;
        if rev == self.revision {
            match reply {
                Some(Reply::Preview(p)) => self.ui.preview = Some(p),
                Some(Reply::List(l, s)) => {
                    self.ui.listing = l;
                    self.ui.loading = false;
                    self.ui.row = s
                        .and_then(|p| self.ui.listing.entries.iter().position(|e| e.path == p))
                        .unwrap_or(0);
                    self.ui.neighbour = self
                        .ui
                        .listing
                        .neighbours
                        .iter()
                        .position(|p| p == &self.ui.directory)
                        .unwrap_or(0)
                        + self.has_parent();
                    self.clamp();
                    if matches!(self.ui.dialog, Some(Dialog::Delete { ready: false, .. })) {
                        self.delete_dialog()
                    }
                    self.preview()
                }
                None => {
                    self.ui.loading = false;
                    self.notify("Cannot read directory or preview", Severity::Error)
                }
            }
        }
        if let Some(j) = self.queued.take() {
            self.spawn(j)
        }
    }
    pub fn timeout(&self) -> Option<Duration> {
        if self.worker.is_some() {
            Some(Duration::from_millis(4))
        } else {
            self.expires
                .map(|t| t.saturating_duration_since(Instant::now()))
        }
    }
    fn notify(&mut self, s: impl Into<String>, severity: Severity) {
        self.ui.toast = Some((s.into(), severity));
        self.expires = Some(Instant::now() + Duration::from_secs(3))
    }
    fn selected(&self) -> Option<PathBuf> {
        self.ui
            .listing
            .entries
            .get(self.ui.row)
            .map(|e| e.path.clone())
    }
    fn has_parent(&self) -> usize {
        usize::from(self.ui.directory.parent().is_some())
    }
    fn reload(&mut self, s: Option<PathBuf>) {
        self.ui.loading = true;
        self.ui.listing = Listing::default();
        self.ui.preview = None;
        self.ui.top = 0;
        self.ui.row = 0;
        self.ui.neighbour_top = 0;
        self.schedule(Job::List(self.ui.directory.clone(), self.hidden, s))
    }
    fn preview(&mut self) {
        self.ui.preview = None;
        self.ui.preview_x = 0;
        self.ui.preview_y = 0;
        if let Some(p) = self.selected() {
            self.schedule(Job::Preview(p))
        }
    }
    fn clamp(&mut self) {
        fn adjust(top: &mut usize, row: usize, n: u16) {
            let n = usize::from(n).max(1);
            *top = (*top).min(row).max((row + 1).saturating_sub(n))
        }
        let [folders, table, _] = ui::panes(self.width, self.height, &self.ui);
        adjust(
            &mut self.ui.top,
            self.ui.row,
            table.height.saturating_sub(3),
        );
        adjust(
            &mut self.ui.neighbour_top,
            self.ui.neighbour,
            folders.height.saturating_sub(2),
        );
    }
    fn permitted(&mut self) -> bool {
        if self.ui.focus == Focus::Folders {
            self.notify(
                "Focus the file list before using file actions.",
                Severity::Warning,
            );
            false
        } else {
            true
        }
    }
    fn target(&mut self) -> Option<PathBuf> {
        if !self.permitted() {
            return None;
        }
        if self.ui.loading {
            self.notify("Folder is still loading; try again.", Severity::Warning)
        }
        self.selected()
    }
    fn edit(&mut self, p: PathBuf) -> Effect {
        if !p.is_file() {
            self.notify("Select a regular file to edit.", Severity::Warning);
            return Effect::None;
        }
        match actions::editor(&p) {
            Ok(c) => Effect::Edit(Box::new(c), p),
            Err(s) => {
                self.notify(format!("Cannot start editor: {s}"), Severity::Error);
                Effect::None
            }
        }
    }
    pub fn after_edit(&mut self, p: PathBuf, r: std::io::Result<ExitStatus>) {
        match r {
            Ok(s) => {
                if !s.success() {
                    self.notify(
                        format!("Editor exited with status {}", s.code().unwrap_or(-1)),
                        Severity::Error,
                    )
                }
                self.reload(Some(p))
            }
            Err(e) => self.notify(format!("Cannot start editor: {e}"), Severity::Error),
        }
    }
    fn enter(&mut self) -> Effect {
        let p = match self.ui.focus {
            Focus::Preview => None,
            Focus::Table => {
                if let Some(e) = self.ui.listing.entries.get(self.ui.row)
                    && !e.directory
                {
                    return self.edit(e.path.clone());
                }
                self.selected()
            }
            Focus::Folders => {
                if self.has_parent() == 1 && self.ui.neighbour == 0 {
                    self.ui.directory.parent().map(|p| p.to_owned())
                } else {
                    self.ui
                        .listing
                        .neighbours
                        .get(self.ui.neighbour.saturating_sub(self.has_parent()))
                        .cloned()
                }
            }
        };
        if let Some(p) = p {
            self.ui.directory = p;
            self.ui.focus = Focus::Table;
            self.reload(None)
        }
        Effect::None
    }
    fn parent(&mut self) {
        let old = self.ui.directory.clone();
        if let Some(p) = old.parent() {
            self.ui.directory = p.to_owned()
        }
        self.reload(Some(old))
    }
    fn move_cursor(&mut self, delta: isize, horizontal: bool) {
        let [_, _, r] = ui::panes(self.width, self.height, &self.ui);
        match self.ui.focus {
            Focus::Preview => {
                let (pos, max) = if horizontal {
                    let w = self
                        .ui
                        .preview
                        .as_ref()
                        .and_then(|p| {
                            p.lines
                                .iter()
                                .map(|s| unicode_width::UnicodeWidthStr::width(s.as_str()))
                                .max()
                        })
                        .unwrap_or(0);
                    (
                        &mut self.ui.preview_x,
                        w.saturating_sub(usize::from(r.width.saturating_sub(2))),
                    )
                } else {
                    let n = self.ui.preview.as_ref().map_or(0, |p| p.lines.len());
                    (
                        &mut self.ui.preview_y,
                        n.saturating_sub(usize::from(r.height.saturating_sub(5))),
                    )
                };
                *pos = pos.saturating_add_signed(delta).min(max)
            }
            Focus::Folders => {
                self.ui.neighbour =
                    self.ui.neighbour.saturating_add_signed(delta).min(
                        (self.ui.listing.neighbours.len() + self.has_parent()).saturating_sub(1),
                    );
                self.clamp()
            }
            Focus::Table => {
                let row = self
                    .ui
                    .row
                    .saturating_add_signed(delta)
                    .min(self.ui.listing.entries.len().saturating_sub(1));
                if row != self.ui.row {
                    self.ui.row = row;
                    self.clamp();
                    self.preview()
                }
            }
        }
    }
    fn name(&mut self, kind: Name, title: &str, value: String) {
        let cursor = value.chars().count();
        self.name = Some(kind);
        self.ui.dialog = Some(Dialog::Name {
            title: title.into(),
            value,
            cursor,
            selected: cursor != 0,
            button: 0,
        })
    }
    fn submit(&mut self, s: String) {
        let result = match self.name.take() {
            None => return,
            Some(Name::Folder) => actions::create(&self.ui.directory, &s, true),
            Some(Name::File) => actions::create(&self.ui.directory, &s, false),
            Some(Name::Rename(p)) => {
                if p.file_name().is_some_and(|n| n == std::ffi::OsStr::new(&s)) {
                    return;
                }
                actions::rename(&p, &s)
            }
            Some(Name::Go) => {
                let p = if s == "~" || s.starts_with("~/") {
                    std::env::var_os("HOME")
                        .map(PathBuf::from)
                        .unwrap_or_default()
                        .join(s.trim_start_matches('~').trim_start_matches('/'))
                } else {
                    self.ui.directory.join(&s)
                };
                if !p.is_dir() {
                    self.notify(format!("Not a directory: {}", p.display()), Severity::Error)
                } else {
                    match p.canonicalize() {
                        Ok(p) => {
                            self.ui.directory = p;
                            self.reload(None)
                        }
                        Err(e) => self.notify(e.to_string(), Severity::Error),
                    }
                }
                return;
            }
        };
        match result {
            Ok(p) => self.reload(Some(p)),
            Err(e) => self.notify(e, Severity::Error),
        }
    }
    fn delete_dialog(&mut self) {
        self.delete = self.selected();
        let name = self
            .delete
            .as_ref()
            .and_then(|p| p.file_name())
            .map_or_else(
                || {
                    if self.ui.loading {
                        "Loading selected file…".into()
                    } else {
                        "No current selection. Cancel and choose a file.".into()
                    }
                },
                |s| s.to_string_lossy().into_owned(),
            );
        self.ui.dialog = Some(Dialog::Delete {
            name,
            button: 0,
            ready: self.delete.is_some(),
        })
    }
    fn accept(&mut self) {
        match self.ui.dialog.take() {
            Some(Dialog::Name { value, .. }) => self.submit(value),
            Some(Dialog::Delete { ready: true, .. }) => {
                if let Some(p) = self.delete.take() {
                    if Some(&p) != self.selected().as_ref() {
                        self.notify(
                            "Selection changed; delete was cancelled.",
                            Severity::Warning,
                        )
                    } else {
                        match actions::remove(&p) {
                            Ok(()) => self.reload(None),
                            Err(e) => self.notify(e, Severity::Error),
                        }
                    }
                }
            }
            _ => {}
        }
    }
    fn insert(&mut self, s: &str) {
        if let Some(Dialog::Name {
            value,
            cursor,
            selected,
            button: 0,
            ..
        }) = &mut self.ui.dialog
        {
            if *selected {
                value.clear();
                *cursor = 0;
                *selected = false
            }
            let idx = value
                .char_indices()
                .nth(*cursor)
                .map_or(value.len(), |(i, _)| i);
            value.insert_str(idx, s);
            *cursor += s.chars().count()
        }
    }
    fn modal_key(&mut self, k: KeyEvent) {
        let Some(d) = &mut self.ui.dialog else { return };
        if k.code == K::Esc {
            self.ui.dialog = None;
            self.delete = None;
            self.name = None;
            return;
        }
        match d {
            Dialog::Help => {
                if k.code == K::Char('?') {
                    self.ui.dialog = None
                }
            }
            Dialog::Delete { button, ready, .. } => match k.code {
                K::BackTab | K::Tab if *ready => *button = 1 - *button,
                K::Enter | K::Char(' ') => {
                    if *button == 0 {
                        self.ui.dialog = None;
                        self.delete = None
                    } else if *ready {
                        self.accept()
                    }
                }
                _ => {}
            },
            Dialog::Name {
                value,
                cursor,
                selected,
                button,
                ..
            } => {
                let len = value.chars().count();
                match k.code {
                    K::Tab => *button = (*button + 1) % 3,
                    K::BackTab => *button = (*button + 2) % 3,
                    K::Enter | K::Char(' ') if k.code == K::Enter || *button != 0 => {
                        if *button == 1 {
                            self.ui.dialog = None;
                            self.name = None
                        } else {
                            self.accept()
                        }
                    }
                    _ if *button != 0 => {}
                    K::Left => {
                        *cursor = if *selected {
                            0
                        } else {
                            cursor.saturating_sub(1)
                        };
                        *selected = false
                    }
                    K::Right => {
                        *cursor = if *selected {
                            len
                        } else {
                            (*cursor + 1).min(len)
                        };
                        *selected = false
                    }
                    K::Home => {
                        *cursor = 0;
                        *selected = false
                    }
                    K::End => {
                        *cursor = len;
                        *selected = false
                    }
                    K::Backspace | K::Delete => {
                        if *selected {
                            value.clear();
                            *cursor = 0;
                            *selected = false
                        } else if k.code == K::Backspace && *cursor > 0
                            || k.code == K::Delete && *cursor < len
                        {
                            if k.code == K::Backspace {
                                *cursor -= 1
                            }
                            if let Some((i, _)) = value.char_indices().nth(*cursor) {
                                value.remove(i);
                            }
                        }
                    }
                    K::Char('a') if k.modifiers == M::CONTROL => {
                        *cursor = 0;
                        *selected = false
                    }
                    K::Char('u') if k.modifiers == M::CONTROL => {
                        if *selected {
                            value.clear()
                        } else {
                            let i = value
                                .char_indices()
                                .nth(*cursor)
                                .map_or(value.len(), |(i, _)| i);
                            value.drain(..i);
                        }
                        *cursor = 0;
                        *selected = false
                    }
                    K::Char(c) if !k.modifiers.intersects(M::ALT | M::CONTROL) => {
                        self.insert(&c.to_string())
                    }
                    _ => {}
                }
            }
        }
    }
    fn cycle(&mut self, back: bool) {
        let areas = ui::panes(self.width, self.height, &self.ui);
        let v: Vec<_> = [
            (Focus::Table, areas[1]),
            (Focus::Folders, areas[0]),
            (Focus::Preview, areas[2]),
        ]
        .into_iter()
        .filter(|(_, r)| r.width > 0)
        .map(|(x, _)| x)
        .collect();
        if let Some(i) = v.iter().position(|s| s == &self.ui.focus) {
            self.ui.focus = v[(i + if back { v.len() - 1 } else { 1 }) % v.len()]
        }
    }
    pub fn handle(&mut self, event: Event, w: u16, h: u16) -> Effect {
        self.width = w;
        self.height = h;
        match event {
            Event::Key(k) if k.kind != KeyEventKind::Release => {
                if k.code == K::Char('q') && k.modifiers == M::CONTROL {
                    return Effect::Quit;
                }
                if self.ui.dialog.is_some() {
                    self.modal_key(k);
                    return Effect::None;
                }
                if !k.modifiers.intersects(M::CONTROL | M::ALT) {
                    match k.code {
                        K::Tab | K::BackTab => self.cycle(k.code == K::BackTab),
                        K::Up => self.move_cursor(-1, false),
                        K::Down => self.move_cursor(1, false),
                        K::Home => self.move_cursor(isize::MIN, false),
                        K::End => self.move_cursor(isize::MAX, false),
                        K::PageUp => self
                            .move_cursor(-isize::try_from(h.saturating_sub(5)).unwrap_or(1), false),
                        K::PageDown => self
                            .move_cursor(isize::try_from(h.saturating_sub(5)).unwrap_or(1), false),
                        K::Left if self.ui.focus == Focus::Preview => self.move_cursor(-1, true),
                        K::Right if self.ui.focus == Focus::Preview => self.move_cursor(1, true),
                        K::Left if self.ui.focus == Focus::Table => self.parent(),
                        K::Backspace | K::Char('h') => self.parent(),
                        K::Char('k') if self.ui.focus == Focus::Table => {
                            self.move_cursor(-1, false)
                        }
                        K::Char('j') if self.ui.focus == Focus::Table => self.move_cursor(1, false),
                        K::Enter => return self.enter(),
                        K::Right | K::Char('l') if self.ui.focus == Focus::Table => {
                            return self.enter();
                        }
                        K::Char('?') => self.ui.dialog = Some(Dialog::Help),
                        K::Char('v') => {
                            if w.saturating_sub(2) < 58 {
                                self.ui.compact_preview = !self.ui.compact_preview;
                                self.ui.focus = if self.ui.compact_preview {
                                    Focus::Preview
                                } else {
                                    Focus::Table
                                }
                            } else {
                                self.ui.focus = if self.ui.focus == Focus::Preview {
                                    Focus::Table
                                } else {
                                    Focus::Preview
                                }
                            }
                        }
                        K::Char('b') => {
                            if w.saturating_sub(2) >= 96 {
                                self.ui.focus = Focus::Folders
                            } else {
                                self.notify(
                                    "Folder column needs more width; use h to browse the parent.",
                                    Severity::Info,
                                )
                            }
                        }
                        K::Char('n') if self.permitted() => {
                            self.name(Name::Folder, "New folder name", String::new())
                        }
                        K::Char('f') if self.permitted() => {
                            self.name(Name::File, "New file name", String::new())
                        }
                        K::Char('g') => self.name(
                            Name::Go,
                            "Go to folder",
                            self.ui.directory.to_string_lossy().into_owned(),
                        ),
                        K::Char('r') => {
                            if let Some(p) = self.target() {
                                let s = p
                                    .file_name()
                                    .unwrap_or_default()
                                    .to_string_lossy()
                                    .into_owned();
                                self.name(Name::Rename(p), "Rename to", s)
                            }
                        }
                        K::Char('e') => {
                            if let Some(p) = self.target() {
                                return self.edit(p);
                            }
                        }
                        K::Char('d')
                            if self.permitted()
                                && (self.selected().is_some() || self.ui.loading) =>
                        {
                            self.delete_dialog()
                        }
                        K::Char(c @ ('x' | 'y')) => {
                            if let Some(p) = self.target() {
                                self.notify(
                                    format!(
                                        "{}: {}",
                                        if c == 'x' { "Cut" } else { "Copied" },
                                        p.file_name().unwrap_or_default().to_string_lossy()
                                    ),
                                    Severity::Info,
                                );
                                self.clip = Some((p, c == 'x'))
                            }
                        }
                        K::Char('p') if self.permitted() => {
                            if let Some((p, m)) = &self.clip {
                                match actions::paste(p, &self.ui.directory, *m) {
                                    Ok(p) => {
                                        if *m {
                                            self.clip = None
                                        }
                                        self.reload(Some(p))
                                    }
                                    Err(e) => self.notify(e, Severity::Error),
                                }
                            } else {
                                self.notify("Copy or cut a file first.", Severity::Warning)
                            }
                        }
                        _ => {}
                    }
                } else if k.code == K::Char('r') && k.modifiers == M::CONTROL {
                    self.reload(self.selected())
                }
            }
            Event::Resize(_, _) => {
                let [folders, table, prev] = ui::panes(w, h, &self.ui);
                if self.ui.focus == Focus::Folders && folders.width == 0
                    || self.ui.focus == Focus::Preview && prev.width == 0
                    || self.ui.focus == Focus::Table && table.width == 0
                {
                    self.ui.focus = if table.width == 0 {
                        Focus::Preview
                    } else {
                        Focus::Table
                    }
                }
                self.clamp()
            }
            Event::Paste(s) => self.insert(&s.replace(['\n', '\r'], " ")),
            Event::Mouse(m) => {
                let hit = ui::hit(&self.ui, w, h, m.column, m.row);
                match m.kind {
                    Mouse::Down(MouseButton::Left) => match hit {
                        Hit::Cancel => {
                            self.ui.dialog = None;
                            self.name = None;
                            self.delete = None
                        }
                        Hit::Submit => {
                            if !matches!(self.ui.dialog, Some(Dialog::Delete { ready: false, .. }))
                            {
                                self.accept()
                            }
                        }
                        Hit::Input => {
                            if let Some(Dialog::Name { button, .. }) = &mut self.ui.dialog {
                                *button = 0
                            }
                        }
                        Hit::Folder(i) => {
                            self.ui.focus = Focus::Folders;
                            self.ui.neighbour = i;
                            return self.enter();
                        }
                        Hit::Preview => self.ui.focus = Focus::Preview,
                        Hit::Table(i) => {
                            let open = self.ui.row == i;
                            self.ui.focus = Focus::Table;
                            self.ui.row = i;
                            if open {
                                return self.enter();
                            }
                            self.preview()
                        }
                        Hit::None => {}
                    },
                    Mouse::ScrollUp | Mouse::ScrollDown if self.ui.dialog.is_none() => {
                        let delta = if m.kind == Mouse::ScrollDown { 3 } else { -3 };
                        match hit {
                            Hit::Table(_) => {
                                self.ui.top = self.ui.top.saturating_add_signed(delta).min(
                                    self.ui
                                        .listing
                                        .entries
                                        .len()
                                        .saturating_sub(usize::from(h.saturating_sub(5))),
                                )
                            }
                            Hit::Preview => {
                                let old = self.ui.focus;
                                self.ui.focus = Focus::Preview;
                                self.move_cursor(delta, false);
                                self.ui.focus = old
                            }
                            Hit::Folder(_) => {
                                self.ui.neighbour_top =
                                    self.ui.neighbour_top.saturating_add_signed(delta).min(
                                        (self.ui.listing.neighbours.len() + self.has_parent())
                                            .saturating_sub(usize::from(h.saturating_sub(4))),
                                    )
                            }
                            _ => {}
                        }
                    }
                    _ => {}
                }
            }
            _ => {}
        }
        Effect::None
    }
}
