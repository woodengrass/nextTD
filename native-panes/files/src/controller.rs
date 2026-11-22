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
    /// Rename target plus the exact dialog value it opened with, so an
    /// unchanged non-UTF8 name (shown lossy) is recognised as untouched.
    Rename(PathBuf, String),
    Go,
}
enum Reply {
    List(Listing, Option<PathBuf>),
    Preview(Preview, PathBuf),
}
/// A filesystem mutation handed to a background thread. Paste and confirmed
/// delete run here so the event thread stays responsive.
enum MutRequest {
    Paste {
        src: PathBuf,
        dir: PathBuf,
        moving: bool,
    },
    Delete {
        path: PathBuf,
    },
}
/// Finished mutation. `dir`/`src` are the context captured at accept time so
/// completion stays correct when the user navigated or re-copied meanwhile.
enum Mutation {
    Paste {
        src: PathBuf,
        dir: PathBuf,
        moving: bool,
        result: Result<PathBuf, String>,
    },
    Delete {
        path: PathBuf,
        result: Result<(), String>,
    },
}
pub struct Controller {
    pub ui: Ui,
    hidden: bool,
    clip: Option<(PathBuf, bool)>,
    name: Option<Name>,
    delete: Option<PathBuf>,
    mutation: Option<Receiver<Mutation>>,
    /// Latest requested (directory, selection): preserves the desired path
    /// across rapid reloads while loading, scoped per directory.
    want: Option<(PathBuf, Option<PathBuf>)>,
    list_rev: u64,
    list_worker: Option<(u64, Receiver<Reply>)>,
    list_queued: Option<(PathBuf, bool, Option<PathBuf>)>,
    preview_rev: u64,
    preview_worker: Option<(u64, Receiver<Reply>)>,
    preview_queued: Option<(u64, PathBuf)>,
    drag: Option<u16>,
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
            mutation: None,
            want: None,
            list_rev: 0,
            list_worker: None,
            list_queued: None,
            preview_rev: 0,
            preview_worker: None,
            preview_queued: None,
            drag: None,
            expires: None,
            width: 80,
            height: 24,
        };
        c.reload(None);
        c
    }
    fn schedule_list(&mut self, dir: PathBuf, hidden: bool, sel: Option<PathBuf>) {
        self.list_rev += 1;
        if self.list_worker.is_some() {
            self.list_queued = Some((dir, hidden, sel));
        } else {
            self.spawn_list(dir, hidden, sel);
        }
    }
    fn spawn_list(&mut self, dir: PathBuf, hidden: bool, sel: Option<PathBuf>) {
        let (tx, rx) = mpsc::channel();
        self.list_worker = Some((self.list_rev, rx));
        std::thread::spawn(move || {
            let _ = tx.send(Reply::List(data::list(&dir, hidden), sel));
        });
    }
    fn schedule_preview(&mut self, path: PathBuf) {
        self.preview_rev += 1;
        if self.preview_worker.is_some() {
            self.preview_queued = Some((self.preview_rev, path));
        } else {
            self.spawn_preview(self.preview_rev, path);
        }
    }
    fn spawn_preview(&mut self, rev: u64, path: PathBuf) {
        let (tx, rx) = mpsc::channel();
        self.preview_worker = Some((rev, rx));
        std::thread::spawn(move || {
            let preview = data::preview(&path);
            let _ = tx.send(Reply::Preview(preview, path));
        });
    }
    pub fn poll(&mut self) {
        if self.expires.is_some_and(|t| Instant::now() >= t) {
            self.expires = None;
            self.ui.toast = None
        }
        if let Some((rev, rx)) = self.list_worker.take() {
            match rx.try_recv() {
                Ok(reply) => {
                    if rev == self.list_rev {
                        match reply {
                            Reply::List(l, s) => {
                                self.ui.listing = l;
                                self.ui.loading = self.mutation.is_some();
                                self.ui.row = s
                                    .and_then(|p| {
                                        self.ui.listing.entries.iter().position(|e| e.path == p)
                                    })
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
                                if matches!(
                                    self.ui.dialog,
                                    Some(Dialog::Delete { ready: false, .. })
                                ) {
                                    self.delete_dialog()
                                }
                                self.preview()
                            }
                            Reply::Preview(..) => {}
                        }
                    }
                }
                Err(mpsc::TryRecvError::Empty) => {
                    self.list_worker = Some((rev, rx));
                }
                Err(_) => {
                    if rev == self.list_rev {
                        self.ui.loading = self.mutation.is_some();
                        self.notify("Cannot read directory or preview", Severity::Error)
                    }
                }
            }
            if self.list_worker.is_none()
                && let Some((dir, hidden, sel)) = self.list_queued.take()
            {
                self.spawn_list(dir, hidden, sel)
            }
        }
        if let Some((rev, rx)) = self.preview_worker.take() {
            match rx.try_recv() {
                Ok(reply) => {
                    if rev == self.preview_rev {
                        match reply {
                            Reply::Preview(p, path) => {
                                if Some(&path) == self.selected().as_ref() {
                                    self.ui.preview = Some(p)
                                }
                            }
                            Reply::List(..) => {}
                        }
                    }
                }
                Err(mpsc::TryRecvError::Empty) => {
                    self.preview_worker = Some((rev, rx));
                }
                Err(_) => {
                    if rev == self.preview_rev {
                        self.notify("Cannot preview file", Severity::Error)
                    }
                }
            }
            if self.preview_worker.is_none()
                && let Some((r, p)) = self.preview_queued.take()
            {
                self.spawn_preview(r, p)
            }
        }
        if let Some(rx) = self.mutation.take() {
            match rx.try_recv() {
                Ok(done) => self.finish_mutation(done),
                Err(mpsc::TryRecvError::Empty) => {
                    self.mutation = Some(rx);
                    if self.ui.toast.is_some() {
                        self.expires = Some(Instant::now() + Duration::from_secs(3))
                    }
                }
                Err(_) => {
                    self.notify("Operation failed.", Severity::Error);
                    self.ui.loading = self.list_worker.is_some()
                }
            }
        }
    }
    pub fn timeout(&self) -> Option<Duration> {
        if self.list_worker.is_some() || self.preview_worker.is_some() || self.mutation.is_some() {
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
        self.drag = None;
        self.ui.loading = true;
        self.ui.listing = Listing::default();
        self.ui.preview = None;
        self.ui.top = 0;
        self.ui.row = 0;
        self.ui.neighbour_top = 0;
        self.preview_rev += 1;
        self.preview_queued = None;
        let dir = self.ui.directory.clone();
        let sel = s.or_else(|| match &self.want {
            Some((d, w)) if *d == dir => w.clone(),
            _ => None,
        });
        self.want = Some((dir.clone(), sel.clone()));
        self.schedule_list(dir, self.hidden, sel)
    }
    fn mutation_busy(&mut self) -> bool {
        if self.mutation.is_some() {
            self.notify("Operation in progress; try again.", Severity::Warning);
            true
        } else {
            false
        }
    }
    fn start_mutation(&mut self, op: MutRequest) {
        let working = match &op {
            MutRequest::Paste { src, moving, .. } => format!(
                "{}: {}",
                if *moving { "Moving" } else { "Copying" },
                src.file_name().unwrap_or_default().to_string_lossy()
            ),
            MutRequest::Delete { path } => format!(
                "Deleting: {}",
                path.file_name().unwrap_or_default().to_string_lossy()
            ),
        };
        let (tx, rx) = mpsc::channel();
        self.mutation = Some(rx);
        self.ui.loading = true;
        self.notify(working, Severity::Info);
        std::thread::spawn(move || {
            let done = match op {
                MutRequest::Paste { src, dir, moving } => {
                    let result = actions::paste(&src, &dir, moving);
                    Mutation::Paste {
                        src,
                        dir,
                        moving,
                        result,
                    }
                }
                MutRequest::Delete { path } => {
                    let result = actions::remove(&path);
                    Mutation::Delete { path, result }
                }
            };
            let _ = tx.send(done);
        });
    }
    fn finish_mutation(&mut self, done: Mutation) {
        match done {
            Mutation::Paste {
                src,
                dir,
                moving,
                result,
            } => match result {
                Ok(target) => {
                    if moving
                        && self
                            .clip
                            .as_ref()
                            .is_some_and(|(p, m)| p == &src && *m == moving)
                    {
                        self.clip = None
                    }
                    self.notify(
                        format!(
                            "{}: {}",
                            if moving { "Moved" } else { "Copied" },
                            target.file_name().unwrap_or_default().to_string_lossy()
                        ),
                        Severity::Info,
                    );
                    self.reload((dir == self.ui.directory).then_some(target));
                }
                Err(e) => {
                    self.notify(e, Severity::Error);
                    self.ui.loading = self.list_worker.is_some()
                }
            },
            Mutation::Delete { path, result } => match result {
                Ok(()) => {
                    self.notify(
                        format!(
                            "Deleted: {}",
                            path.file_name().unwrap_or_default().to_string_lossy()
                        ),
                        Severity::Info,
                    );
                    self.reload(self.selected());
                }
                Err(e) => {
                    self.notify(e, Severity::Error);
                    self.ui.loading = self.list_worker.is_some()
                }
            },
        }
    }
    fn preview(&mut self) {
        self.ui.preview = None;
        self.ui.preview_x = 0;
        self.ui.preview_y = 0;
        self.preview_queued = None;
        if let Some(p) = self.selected() {
            self.schedule_preview(p)
        }
    }
    fn preview_max(&self) -> (usize, usize) {
        let panes = ui::panes(self.width, self.height, &self.ui);
        let rows = ui::preview_rows(&self.ui, panes[2]);
        let max_y = self
            .ui
            .preview
            .as_ref()
            .map_or(0, |p| p.lines.len())
            .saturating_sub(rows);
        let content = self.ui.preview.as_ref().map_or(0, |p| {
            p.lines
                .iter()
                .map(|s| unicode_width::UnicodeWidthStr::width(s.as_str()))
                .max()
                .unwrap_or(0)
        });
        let max_x = content.saturating_sub(usize::from(panes[2].width.saturating_sub(2)));
        (max_x, max_y)
    }
    fn page_by(&mut self, dir: isize) {
        let table = ui::panes(self.width, self.height, &self.ui)[1];
        let page = usize::from(table.height.saturating_sub(3)).max(1);
        if let Some(sb) = ui::table_scrollbar(&self.ui, self.width, self.height) {
            self.ui.top = self
                .ui
                .top
                .saturating_add_signed(dir * page as isize)
                .min(sb.max_top);
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
        let (max_x, max_y) = self.preview_max();
        self.ui.preview_x = self.ui.preview_x.min(max_x);
        self.ui.preview_y = self.ui.preview_y.min(max_y);
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
        if self.mutation_busy() {
            return Effect::None;
        }
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
        match self.ui.focus {
            Focus::Preview => {
                let (max_x, max_y) = self.preview_max();
                let (pos, max) = if horizontal {
                    (&mut self.ui.preview_x, max_x)
                } else {
                    (&mut self.ui.preview_y, max_y)
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
        let kind = self.name.take();
        if !matches!(kind, Some(Name::Go) | None) && self.mutation_busy() {
            return;
        }
        let result = match kind {
            None => return,
            Some(Name::Folder) => actions::create(&self.ui.directory, &s, true),
            Some(Name::File) => actions::create(&self.ui.directory, &s, false),
            Some(Name::Rename(p, initial)) => {
                if s == initial {
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
                if self.mutation_busy() {
                } else if let Some(p) = self.delete.take() {
                    if Some(&p) != self.selected().as_ref() {
                        self.notify(
                            "Selection changed; delete was cancelled.",
                            Severity::Warning,
                        )
                    } else {
                        self.start_mutation(MutRequest::Delete { path: p });
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
                    if self.mutation.is_some() {
                        self.notify(
                            "An operation is still running; try again when it finishes.",
                            Severity::Warning,
                        );
                        return Effect::None;
                    }
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
                        K::Char('n') if self.permitted() && !self.mutation_busy() => {
                            self.name(Name::Folder, "New folder name", String::new())
                        }
                        K::Char('f') if self.permitted() && !self.mutation_busy() => {
                            self.name(Name::File, "New file name", String::new())
                        }
                        K::Char('g') => self.name(
                            Name::Go,
                            "Go to folder",
                            self.ui.directory.to_string_lossy().into_owned(),
                        ),
                        K::Char('r') => {
                            if let Some(p) = self.target() {
                                let initial = p
                                    .file_name()
                                    .unwrap_or_default()
                                    .to_string_lossy()
                                    .into_owned();
                                self.name(Name::Rename(p, initial.clone()), "Rename to", initial)
                            }
                        }
                        K::Char('e') => {
                            if let Some(p) = self.target() {
                                return self.edit(p);
                            }
                        }
                        K::Char('d')
                            if self.permitted()
                                && (self.selected().is_some() || self.ui.loading)
                                && !self.mutation_busy() =>
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
                            if self.mutation_busy() {
                            } else if let Some((p, m)) = self.clip.clone() {
                                self.start_mutation(MutRequest::Paste {
                                    src: p,
                                    dir: self.ui.directory.clone(),
                                    moving: m,
                                })
                            } else {
                                self.notify("Copy or cut a file first.", Severity::Warning)
                            }
                        }
                        K::Char('.') => {
                            self.hidden = !self.hidden;
                            self.reload(self.selected())
                        }
                        _ => {}
                    }
                } else if k.code == K::Char('r') && k.modifiers == M::CONTROL {
                    self.reload(self.selected())
                }
            }
            Event::Resize(_, _) => {
                self.drag = None;
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
                    Mouse::Down(MouseButton::Left) => {
                        self.drag = None;
                        match hit {
                            Hit::Cancel => {
                                self.ui.dialog = None;
                                self.name = None;
                                self.delete = None
                            }
                            Hit::Submit => {
                                if !matches!(
                                    self.ui.dialog,
                                    Some(Dialog::Delete { ready: false, .. })
                                ) {
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
                            Hit::TableScrollbar => {
                                if let Some(sb) = ui::table_scrollbar(&self.ui, w, h) {
                                    let bottom = sb.thumb.y.saturating_add(sb.thumb.height);
                                    if m.row >= sb.thumb.y && m.row < bottom {
                                        self.drag = Some(m.row - sb.thumb.y);
                                    } else if m.row < sb.thumb.y {
                                        self.page_by(-1);
                                    } else {
                                        self.page_by(1);
                                    }
                                }
                            }
                            Hit::None => {}
                        }
                    }
                    Mouse::Drag(MouseButton::Left) => {
                        if let (Some(off), Some(sb)) =
                            (self.drag, ui::table_scrollbar(&self.ui, w, h))
                        {
                            self.ui.top = sb
                                .top_for_thumb_y(m.row.saturating_sub(off))
                                .min(sb.max_top);
                        }
                    }
                    Mouse::Up(MouseButton::Left) => self.drag = None,
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

#[cfg(test)]
mod controller_tests {
    //! Behavioral tests for [`super::Controller`]. List and preview lanes use
    //! real threads; [`settle`] spins `poll()` with no sleeps until every
    //! lane (including mutation) is idle, and lane-contention tests install
    //! stub channels whose senders the test itself controls.

    use super::*;
    use crossterm::event::MouseEvent;

    fn test_dir(files: &[(&str, &str)]) -> tempfile::TempDir {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        for (name, content) in files {
            std::fs::write(tmp.path().join(name), content).expect("write fixture");
        }
        tmp
    }

    fn many_files(n: usize) -> (tempfile::TempDir, Vec<PathBuf>) {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let mut paths = Vec::new();
        for i in 0..n {
            let name = format!("f{i:02}.txt");
            std::fs::write(tmp.path().join(&name), "x").expect("write fixture");
            paths.push(tmp.path().join(name));
        }
        (tmp, paths)
    }

    fn settle(c: &mut Controller) {
        for _ in 0..10_000_000 {
            c.poll();
            if c.list_worker.is_none()
                && c.preview_worker.is_none()
                && c.mutation.is_none()
                && c.list_queued.is_none()
                && c.preview_queued.is_none()
                && !c.ui.loading
            {
                return;
            }
            std::thread::yield_now();
        }
        panic!("workers did not settle");
    }

    fn key(code: K, modifiers: M) -> Event {
        Event::Key(KeyEvent::new(code, modifiers))
    }

    fn mouse(kind: Mouse, column: u16, row: u16) -> Event {
        Event::Mouse(MouseEvent {
            kind,
            column,
            row,
            modifiers: M::empty(),
        })
    }

    #[test]
    fn dot_toggles_hidden_and_keeps_selection() {
        let tmp = test_dir(&[("b.txt", "b"), ("a.txt", "a"), (".hidden", "h")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        assert!(
            !c.ui
                .listing
                .entries
                .iter()
                .any(|e| e.path.ends_with(".hidden"))
        );
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        let selected = tmp.path().join("b.txt");
        assert_eq!(c.selected(), Some(selected.clone()));

        c.handle(key(K::Char('.'), M::empty()), 80, 24);
        settle(&mut c);
        assert!(
            c.ui.listing
                .entries
                .iter()
                .any(|e| e.path.ends_with(".hidden"))
        );
        assert_eq!(c.selected(), Some(selected.clone()));

        c.handle(key(K::Char('.'), M::empty()), 80, 24);
        settle(&mut c);
        assert!(
            !c.ui
                .listing
                .entries
                .iter()
                .any(|e| e.path.ends_with(".hidden"))
        );
        assert_eq!(c.selected(), Some(selected));
    }

    #[test]
    fn blocked_preview_does_not_delay_navigation() {
        let tmp = test_dir(&[("a.txt", "a")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        // Install a preview lane that never answers; the held sender keeps
        // `try_recv` at `Empty` instead of disconnecting.
        let (tx, rx) = mpsc::channel::<Reply>();
        c.preview_worker = Some((c.preview_rev, rx));

        c.handle(key(K::Char('r'), M::CONTROL), 80, 24);
        assert!(
            c.list_worker.is_some(),
            "list lane must dispatch while preview is blocked"
        );
        c.poll();
        assert!(
            c.preview_worker.is_some(),
            "blocked preview lane must be left alone"
        );

        drop(tx);
        settle(&mut c);
        assert!(!c.ui.loading);
        assert!(!c.ui.listing.entries.is_empty());
    }

    #[test]
    fn stale_preview_reply_is_dropped_after_reload() {
        let tmp = test_dir(&[("a.txt", "hello")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        let selected = c.selected().expect("selection");
        assert!(c.ui.preview.is_some());

        let (tx, rx) = mpsc::channel::<Reply>();
        tx.send(Reply::Preview(
            Preview {
                meta: vec!["stale".into()],
                lines: vec!["stale".into()],
            },
            selected.clone(),
        ))
        .expect("send");
        c.preview_worker = Some((c.preview_rev, rx));
        c.reload(Some(selected.clone()));
        assert!(c.ui.preview.is_none());
        c.poll();
        assert!(
            c.ui.preview.is_none(),
            "stale preview reply must be dropped after reload"
        );

        settle(&mut c);
        assert_eq!(c.selected(), Some(selected));
        assert!(c.ui.preview.is_some());
    }

    #[test]
    fn wheel_scroll_keeps_selection() {
        let (_tmp, _) = many_files(40);
        let mut c = Controller::new(_tmp.path().to_path_buf());
        settle(&mut c);
        let selected = c.selected();
        let top = c.ui.top;
        c.handle(mouse(Mouse::ScrollDown, 2, 6), 80, 24);
        assert_eq!(c.selected(), selected);
        assert_eq!(c.ui.top, top + 3);
    }

    #[test]
    fn table_scrollbar_drag_scrolls_without_selecting() {
        let (_tmp, _) = many_files(40);
        let mut c = Controller::new(_tmp.path().to_path_buf());
        settle(&mut c);
        let selected = c.selected();
        let sb = ui::table_scrollbar(&c.ui, 80, 24).expect("scrollbar when list overflows");
        c.handle(
            mouse(
                Mouse::Down(MouseButton::Left),
                sb.track.x,
                sb.track.y + sb.track.height.saturating_sub(1),
            ),
            80,
            24,
        );
        assert_eq!(c.selected(), selected);
        assert!(c.ui.top > 0);

        let sb = ui::table_scrollbar(&c.ui, 80, 24).expect("scrollbar when list overflows");
        c.handle(
            mouse(Mouse::Down(MouseButton::Left), sb.thumb.x, sb.thumb.y),
            80,
            24,
        );
        assert_eq!(c.selected(), selected);
        assert_eq!(c.drag, Some(0));
        let bottom = sb.track.y + sb.track.height.saturating_sub(1);
        c.handle(mouse(Mouse::Drag(MouseButton::Left), 1, bottom), 80, 24);
        assert_eq!(c.selected(), selected);
        assert_eq!(c.ui.top, sb.top_for_thumb_y(bottom).min(sb.max_top));

        c.handle(mouse(Mouse::Up(MouseButton::Left), 1, bottom), 80, 24);
        assert_eq!(c.drag, None);
        let top = c.ui.top;
        c.handle(
            mouse(Mouse::Drag(MouseButton::Left), sb.track.x, sb.track.y),
            80,
            24,
        );
        assert_eq!(c.ui.top, top);
        assert_eq!(c.selected(), selected);
    }

    #[test]
    fn rapid_hidden_toggles_preserve_desired_selection() {
        let tmp = test_dir(&[("b.txt", "b"), ("a.txt", "a"), (".hidden", "h")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        let selected = tmp.path().join("b.txt");
        assert_eq!(c.selected(), Some(selected.clone()));

        let (tx, rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, rx));
        c.handle(key(K::Char('.'), M::empty()), 80, 24);
        c.handle(key(K::Char('.'), M::empty()), 80, 24);
        tx.send(Reply::List(Listing::default(), None))
            .expect("send");
        settle(&mut c);

        assert!(
            !c.ui
                .listing
                .entries
                .iter()
                .any(|e| e.path.ends_with(".hidden"))
        );
        assert_eq!(c.selected(), Some(selected));
    }

    #[test]
    fn reload_before_settle_does_not_leak_selection_across_directories() {
        let tmp = test_dir(&[("a.txt", "a")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        std::fs::write(tmp.path().join("sub").join("c.txt"), "c").expect("write");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        assert_eq!(c.selected(), Some(tmp.path().join("a.txt")));

        c.handle(key(K::Char('y'), M::empty()), 80, 24);
        c.handle(key(K::Up, M::empty()), 80, 24);
        settle(&mut c);
        c.handle(key(K::Enter, M::empty()), 80, 24);
        assert_eq!(c.ui.directory, tmp.path().join("sub"));
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some());
        c.handle(key(K::Char('h'), M::empty()), 80, 24);
        settle(&mut c);

        assert_eq!(c.ui.directory, tmp.path());
        assert!(tmp.path().join("a.txt").exists());
        assert!(tmp.path().join("sub").join("a.txt").exists());
        assert_eq!(c.selected(), Some(tmp.path().join("sub")));
        assert!(c.clip.is_some(), "copy keeps the clipboard");
    }

    #[test]
    fn unchanged_non_utf8_rename_is_noop_but_edit_renames() {
        use std::os::unix::ffi::OsStrExt as _;
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let raw = tmp
            .path()
            .join(std::ffi::OsStr::from_bytes(b"bad\xffname.txt"));
        std::fs::write(&raw, "x").expect("write");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(raw.clone()));

        c.handle(key(K::Char('r'), M::empty()), 80, 24);
        c.handle(key(K::Enter, M::empty()), 80, 24);
        assert!(raw.exists());
        assert!(!tmp.path().join("bad\u{FFFD}name.txt").exists());
        assert_eq!(c.ui.listing.entries.len(), 1);

        c.handle(key(K::Char('r'), M::empty()), 80, 24);
        c.handle(key(K::Char('z'), M::empty()), 80, 24);
        c.handle(key(K::Enter, M::empty()), 80, 24);
        settle(&mut c);
        assert!(!raw.exists());
        assert!(tmp.path().join("z").exists());
        assert_eq!(c.selected(), Some(tmp.path().join("z")));
    }

    #[test]
    fn paste_runs_off_thread_and_quit_waits_for_it() {
        let tmp = test_dir(&[("a.txt", "a")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        c.handle(key(K::Char('y'), M::empty()), 80, 24);
        c.handle(key(K::Up, M::empty()), 80, 24);
        settle(&mut c);
        c.handle(key(K::Enter, M::empty()), 80, 24);
        assert_eq!(c.ui.directory, tmp.path().join("sub"));

        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "paste must leave a pending mutation");
        assert!(c.ui.loading, "mutation must show busy until done");
        assert!(
            c.timeout().is_some(),
            "repaint must keep polling while busy"
        );
        assert!(
            matches!(
                c.handle(key(K::Char('q'), M::CONTROL), 80, 24),
                Effect::None
            ),
            "quit must wait for the outstanding mutation"
        );
        assert!(c.ui.toast.is_some());
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "conflicting paste must not start");
        assert!(
            matches!(
                c.handle(key(K::Char('e'), M::empty()), 80, 24),
                Effect::None
            ),
            "editor must not launch while a mutation runs"
        );

        settle(&mut c);
        assert!(tmp.path().join("sub").join("a.txt").exists());
        assert!(c.clip.is_some(), "copy keeps the clipboard");
        assert_eq!(c.ui.directory, tmp.path().join("sub"));
        assert!(!c.ui.loading);
        assert!(
            matches!(
                c.handle(key(K::Char('q'), M::CONTROL), 80, 24),
                Effect::Quit
            ),
            "quit works once the mutation finished"
        );
    }

    #[test]
    fn confirmed_delete_runs_off_thread_and_stays_correct_across_navigation() {
        let tmp = test_dir(&[("a.txt", "a"), ("b.txt", "b")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        assert_eq!(c.selected(), Some(tmp.path().join("a.txt")));

        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        c.handle(key(K::Tab, M::empty()), 80, 24);
        c.handle(key(K::Char(' '), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "delete must leave a pending mutation");
        assert!(c.ui.loading, "mutation must show busy until done");
        assert!(
            matches!(
                c.handle(key(K::Char('q'), M::CONTROL), 80, 24),
                Effect::None
            ),
            "quit must wait for the outstanding mutation"
        );

        c.handle(key(K::Down, M::empty()), 80, 24);
        assert_eq!(c.ui.row, 2, "navigation stays responsive while busy");
        settle(&mut c);
        assert!(!tmp.path().join("a.txt").exists());
        assert!(tmp.path().join("b.txt").exists());
        assert_eq!(c.selected(), Some(tmp.path().join("b.txt")));
        assert!(matches!(
            c.handle(key(K::Char('q'), M::CONTROL), 80, 24),
            Effect::Quit
        ));
    }

    #[test]
    fn cut_paste_keeps_clipboard_result_when_clip_changed_midflight() {
        let tmp = test_dir(&[("a.txt", "a")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        c.handle(key(K::Char('x'), M::empty()), 80, 24);
        c.handle(key(K::Up, M::empty()), 80, 24);
        settle(&mut c);
        c.handle(key(K::Enter, M::empty()), 80, 24);
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some());

        let elsewhere = tmp.path().join("sub");
        c.clip = Some((elsewhere.clone(), false));
        settle(&mut c);
        assert!(!tmp.path().join("a.txt").exists());
        assert!(tmp.path().join("sub").join("a.txt").exists());
        assert_eq!(c.clip, Some((elsewhere, false)));
    }

    #[test]
    fn preview_end_and_resize_follow_preview_rows() {
        let content = (0..200)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let tmp = test_dir(&[("big.txt", &content)]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        assert!(c.ui.preview.is_some());
        c.handle(key(K::Char('v'), M::empty()), 80, 12);
        assert!(c.ui.focus == Focus::Preview);

        c.handle(key(K::End, M::empty()), 80, 12);
        let rows = ui::preview_rows(&c.ui, ui::panes(80, 12, &c.ui)[2]);
        let max =
            c.ui.preview
                .as_ref()
                .map_or(0, |p| p.lines.len())
                .saturating_sub(rows);
        assert_eq!(c.ui.preview_y, max);

        c.handle(Event::Resize(80, 24), 80, 24);
        let rows = ui::preview_rows(&c.ui, ui::panes(80, 24, &c.ui)[2]);
        let max =
            c.ui.preview
                .as_ref()
                .map_or(0, |p| p.lines.len())
                .saturating_sub(rows);
        assert_eq!(c.ui.preview_y, max);
    }
}
