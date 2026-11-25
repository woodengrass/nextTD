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
        identity: actions::Identity,
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
#[cfg(test)]
#[derive(Debug, Default)]
pub(crate) struct MutationGate {
    open: std::sync::Mutex<bool>,
    arrived: std::sync::atomic::AtomicBool,
    cvar: std::sync::Condvar,
}
#[cfg(test)]
impl MutationGate {
    fn new_closed() -> Self {
        Self {
            open: std::sync::Mutex::new(false),
            arrived: std::sync::atomic::AtomicBool::new(false),
            cvar: std::sync::Condvar::new(),
        }
    }
    fn arrive_and_wait(&self) {
        self.arrived
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let guard = self.open.lock().expect("gate lock");
        let _ = self
            .cvar
            .wait_timeout_while(guard, std::time::Duration::from_secs(5), |open| !*open)
            .expect("gate wait");
    }
    fn open(&self) {
        *self.open.lock().expect("gate lock") = true;
        self.cvar.notify_all();
    }
    fn has_arrived(&self) -> bool {
        self.arrived.load(std::sync::atomic::Ordering::SeqCst)
    }
}
pub struct Controller {
    pub ui: Ui,
    hidden: bool,
    clip: Option<(PathBuf, bool)>,
    name: Option<Name>,
    delete: Option<(PathBuf, actions::Identity)>,
    mutation: Option<Receiver<Mutation>>,
    #[cfg(test)]
    test_gate: Option<std::sync::Arc<MutationGate>>,
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
            #[cfg(test)]
            test_gate: None,
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
        self.ui.toast = Some((data::single_line(s.into()), severity));
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
            MutRequest::Delete { path, .. } => format!(
                "Deleting: {}",
                path.file_name().unwrap_or_default().to_string_lossy()
            ),
        };
        let (tx, rx) = mpsc::channel();
        self.mutation = Some(rx);
        self.ui.loading = true;
        self.notify(working, Severity::Info);
        #[cfg(test)]
        let gate = self.test_gate.clone();
        std::thread::spawn(move || {
            #[cfg(test)]
            if let Some(g) = gate.as_ref() {
                g.arrive_and_wait();
            }
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
                MutRequest::Delete { path, identity } => {
                    let result = actions::remove_confirmed(&path, identity);
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
                    if moving {
                        self.refresh_after_failure();
                    } else {
                        self.ui.loading = self.list_worker.is_some();
                    }
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
                    self.refresh_after_failure()
                }
            },
        }
    }
    fn refresh_after_failure(&mut self) {
        if self.clip.as_ref().is_some_and(|(p, _)| {
            std::fs::symlink_metadata(p).is_err_and(|e| {
                matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                )
            })
        }) {
            self.clip = None;
        }
        self.reload(self.selected());
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
        self.delete = self.selected().and_then(|p| match actions::identity(&p) {
            Ok(id) => Some((p, id)),
            Err(e) => {
                self.notify(e, Severity::Error);
                None
            }
        });
        let name = self
            .delete
            .as_ref()
            .and_then(|(p, _)| p.file_name())
            .map_or_else(
                || {
                    if self.ui.loading {
                        "Loading selected file…".into()
                    } else {
                        "No current selection. Cancel and choose a file.".into()
                    }
                },
                data::single_line,
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
                } else if let Some((p, identity)) = self.delete.take() {
                    if Some(&p) != self.selected().as_ref() {
                        self.notify(
                            "Selection changed; delete was cancelled.",
                            Severity::Warning,
                        )
                    } else {
                        self.start_mutation(MutRequest::Delete { path: p, identity });
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
    fn late_preview_cannot_replace_newer_selection() {
        let tmp = test_dir(&[("a.txt", "old"), ("b.txt", "middle"), ("c.txt", "new")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        let old = tmp.path().join("a.txt");
        let (tx, rx) = mpsc::channel::<Reply>();
        c.preview_worker = Some((c.preview_rev, rx));
        c.handle(key(K::Down, M::empty()), 80, 24);
        c.handle(key(K::Down, M::empty()), 80, 24);
        assert_eq!(c.selected(), Some(tmp.path().join("c.txt")));
        tx.send(Reply::Preview(crate::data::preview(&old), old))
            .unwrap();
        settle(&mut c);
        let preview = c.ui.preview.as_ref().expect("new preview");
        assert!(preview.lines.iter().any(|line| line.contains("new")));
        assert!(!preview.lines.iter().any(|line| line.contains("old")));
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

    fn wait_gate(gate: &super::MutationGate) {
        for _ in 0..10_000_000 {
            if gate.has_arrived() {
                return;
            }
            std::thread::yield_now();
        }
        panic!("mutation worker never reached the gate");
    }

    fn help_is_rendered(c: &Controller) -> bool {
        if !matches!(c.ui.dialog, Some(crate::ui::Dialog::Help)) {
            return false;
        }
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(80, 24)).expect("backend");
        terminal
            .draw(|frame| crate::ui::draw(frame, &c.ui))
            .expect("draw");
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>()
            .contains("File shortcuts")
    }

    #[test]
    fn paste_shows_help_before_completion() {
        use std::sync::Arc as _Arc;
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
        let target = tmp.path().join("sub").join("a.txt");
        assert!(!target.exists());

        let gate = _Arc::new(super::MutationGate::new_closed());
        c.test_gate = Some(gate.clone());
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "paste must leave a pending mutation");
        wait_gate(&gate);
        // B1: help is served while the worker is paused ahead of real work.
        assert!(
            !target.exists(),
            "paste must not complete while the worker is paused"
        );
        c.handle(key(K::Char('?'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(crate::ui::Dialog::Help)),
            "pressing ? must show help while paste is pending"
        );
        assert!(help_is_rendered(&c), "help must be user-visible");
        assert!(
            !target.exists(),
            "filesystem result must still be pending while help is shown"
        );
        assert!(c.mutation.is_some(), "mutation must still be pending");
        // B3: releasing the gate completes the real copy.
        gate.open();
        settle(&mut c);
        assert!(target.exists(), "paste must copy the file after release");
        assert_eq!(std::fs::read(&target).expect("copied file"), b"a");
    }

    #[test]
    fn confirmed_delete_shows_help_before_completion() {
        use std::sync::Arc as _Arc;
        let tmp = test_dir(&[("a.txt", "a"), ("b.txt", "b")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        assert_eq!(c.selected(), Some(tmp.path().join("a.txt")));
        let victim = tmp.path().join("a.txt");

        let gate = _Arc::new(super::MutationGate::new_closed());
        c.test_gate = Some(gate.clone());
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        c.handle(key(K::Tab, M::empty()), 80, 24);
        c.handle(key(K::Char(' '), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "delete must leave a pending mutation");
        wait_gate(&gate);
        // B2: help is served while the worker is paused ahead of real work.
        assert!(
            victim.exists(),
            "delete must not complete while the worker is paused"
        );
        c.handle(key(K::Char('?'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(crate::ui::Dialog::Help)),
            "pressing ? must show help while delete is pending"
        );
        assert!(help_is_rendered(&c), "help must be user-visible");
        assert!(
            victim.exists(),
            "filesystem target must still exist while help is shown"
        );
        assert!(c.mutation.is_some(), "mutation must still be pending");
        // B3: releasing the gate completes the real delete.
        gate.open();
        settle(&mut c);
        assert!(
            !victim.exists(),
            "delete must remove the file after release"
        );
        assert!(tmp.path().join("b.txt").exists());
    }

    #[test]
    fn confirmed_delete_preserves_same_name_replacement() {
        let tmp = test_dir(&[("a", "old")]);
        let mut c = Controller::new(tmp.path().to_owned());
        settle(&mut c);
        let gate = std::sync::Arc::new(super::MutationGate::new_closed());
        c.test_gate = Some(gate.clone());
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        c.handle(key(K::Tab, M::empty()), 80, 24);
        c.handle(key(K::Char(' '), M::empty()), 80, 24);
        wait_gate(&gate);
        let name = tmp.path().join("a");
        std::fs::remove_file(&name).unwrap();
        std::fs::create_dir(&name).unwrap();
        std::fs::write(name.join("keep"), "new").unwrap();
        gate.open();
        settle(&mut c);
        assert_eq!(std::fs::read(name.join("keep")).unwrap(), b"new");
        assert!(c.ui.toast.unwrap().0.contains("changed"));
    }

    fn cross_device_dirs() -> (tempfile::TempDir, tempfile::TempDir) {
        use std::os::unix::fs::MetadataExt as _;
        // Two genuine devices: the default tempdir and a tmpfs one. A
        // missing second device fails loudly, never as a silent same-device
        // pass.
        let src_tmp = tempfile::TempDir::new().expect("tempdir");
        let dst_tmp = tempfile::TempDir::new_in("/dev/shm")
            .expect("needs a second writable device at /dev/shm for the cross-device move check");
        let src_dev = std::fs::metadata(src_tmp.path()).expect("stat src").dev();
        let dst_dev = std::fs::metadata(dst_tmp.path()).expect("stat dst").dev();
        assert!(
            src_dev != dst_dev,
            "cross-device check needs two distinct devices, got same dev {src_dev}"
        );
        (src_tmp, dst_tmp)
    }

    fn go_to(c: &mut Controller, dest: &std::path::Path) {
        c.handle(key(K::Char('g'), M::empty()), 80, 24);
        for ch in dest.to_string_lossy().chars() {
            c.handle(key(K::Char(ch), M::empty()), 80, 24);
        }
        c.handle(key(K::Enter, M::empty()), 80, 24);
        settle(c);
        assert_eq!(c.ui.directory, dest);
    }

    #[test]
    fn cut_paste_across_devices_modified_source_reports_partial_move() {
        let (src_tmp, dst_tmp) = cross_device_dirs();
        let tree = src_tmp.path().join("tree");
        std::fs::create_dir(&tree).expect("mkdir");
        std::fs::create_dir(tree.join("nested")).expect("mkdir");
        std::fs::write(tree.join("a.txt"), "old-a").expect("write");
        std::fs::write(tree.join("nested").join("b.txt"), "old-b").expect("write");

        let mut c = Controller::new(src_tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(tree.clone()));

        // Keys: cut, go-to-folder with a typed cross-device path, paste.
        c.handle(key(K::Char('x'), M::empty()), 80, 24);
        let dest = dst_tmp.path().canonicalize().expect("canonicalize dest");
        go_to(&mut c, &dest);

        // Benign injection after the exclusive copy, before guarded cleanup:
        // the nested file gains new bytes while the destination already
        // holds the old ones.
        let hook_tree = tree.clone();
        crate::actions::stash_between_note(&tree, move || {
            std::fs::write(hook_tree.join("nested").join("b.txt"), "new-bytes")
                .expect("inject modification");
        });
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "paste must leave a pending mutation");
        // A new clipboard selection while the move runs points to the child
        // that will be unlinked before its sibling triggers the conflict.
        c.clip = Some((tree.join("a.txt"), false));
        settle(&mut c);

        assert_eq!(
            c.selected(),
            Some(dest.join("tree")),
            "reload the destination listing"
        );
        assert!(c.clip.is_none(), "drop the missing clipboard source");
        // The partial move is user-visible.
        let (toast, _) =
            c.ui.toast
                .clone()
                .expect("partial-move error must be visible");
        assert!(toast.contains("Partial move"), "unexpected toast: {toast}");
        // The background worker copied to the accept-time (correct) target
        // with the old bytes.
        let target = dest.join("tree");
        assert_eq!(
            std::fs::read(target.join("a.txt")).expect("dest a"),
            b"old-a"
        );
        assert_eq!(
            std::fs::read(target.join("nested").join("b.txt")).expect("dest b"),
            b"old-b"
        );
        // The source keeps the new bytes: the changed file stays, while the
        // unchanged entry already completed the move.
        assert!(
            !tree.join("a.txt").exists(),
            "unchanged source entry must have completed the move"
        );
        assert_eq!(
            std::fs::read(tree.join("nested").join("b.txt")).expect("src b"),
            b"new-bytes"
        );
    }

    #[test]
    fn cut_paste_across_devices_added_source_entry_reports_partial_move() {
        let (src_tmp, dst_tmp) = cross_device_dirs();
        let tree = src_tmp.path().join("tree");
        std::fs::create_dir(&tree).expect("mkdir");
        std::fs::create_dir(tree.join("nested")).expect("mkdir");
        std::fs::write(tree.join("a.txt"), "old-a").expect("write");
        std::fs::write(tree.join("nested").join("b.txt"), "old-b").expect("write");

        let mut c = Controller::new(src_tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(tree.clone()));

        // Keys: cut, go-to-folder with a typed cross-device path, paste.
        c.handle(key(K::Char('x'), M::empty()), 80, 24);
        let dest = dst_tmp.path().canonicalize().expect("canonicalize dest");
        go_to(&mut c, &dest);

        // Benign injection after the exclusive copy, before guarded cleanup:
        // a newly created entry in the source tree, independent of any
        // modification to existing files.
        let hook_tree = tree.clone();
        crate::actions::stash_between_note(&tree, move || {
            std::fs::write(hook_tree.join("new.txt"), "new").expect("inject new file");
        });
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "paste must leave a pending mutation");
        settle(&mut c);

        // The partial move is user-visible.
        let (toast, _) =
            c.ui.toast
                .clone()
                .expect("partial-move error must be visible");
        assert!(toast.contains("Partial move"), "unexpected toast: {toast}");
        // The background worker copied to the accept-time (correct) target
        // with the old bytes, before the new entry existed.
        let target = dest.join("tree");
        assert_eq!(
            std::fs::read(target.join("a.txt")).expect("dest a"),
            b"old-a"
        );
        assert_eq!(
            std::fs::read(target.join("nested").join("b.txt")).expect("dest b"),
            b"old-b"
        );
        assert!(
            !target.join("new.txt").exists(),
            "destination must not contain the entry created after the copy"
        );
        // The source tree is intact and keeps the new entry.
        assert_eq!(std::fs::read(tree.join("a.txt")).expect("src a"), b"old-a");
        assert_eq!(
            std::fs::read(tree.join("nested").join("b.txt")).expect("src b"),
            b"old-b"
        );
        assert_eq!(
            std::fs::read(tree.join("new.txt")).expect("src new"),
            b"new"
        );
    }

    #[test]
    fn copy_tree_via_keys_creates_private_modes_before_final_metadata() {
        use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
        let src_tmp = tempfile::TempDir::new().expect("tempdir");
        let dst_tmp = tempfile::TempDir::new().expect("tempdir");
        let tree = src_tmp.path().join("tree");
        std::fs::create_dir(&tree).expect("mkdir");
        let nested = tree.join("nested");
        std::fs::create_dir(&nested).expect("mkdir");
        let inner = nested.join("b.txt");
        std::fs::write(&inner, "b").expect("write");
        // The transferred source stays private; creation privacy is observed
        // at the caller checkpoint, final preservation afterwards.
        for dir in [&tree, &nested] {
            std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700)).expect("chmod");
        }
        std::fs::set_permissions(&inner, std::fs::Permissions::from_mode(0o600)).expect("chmod");

        let mut c = Controller::new(src_tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(tree.clone()));

        // Keys: copy, go-to-folder with a typed path, paste.
        c.handle(key(K::Char('y'), M::empty()), 80, 24);
        let dest = dst_tmp.path().canonicalize().expect("canonicalize dest");
        go_to(&mut c, &dest);
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "paste must leave a pending mutation");
        settle(&mut c);

        // Creation-time evidence for root, nested dir, and file, taken from
        // the keyboard-originated copy through the worker.
        let taken = crate::actions::take_initial_modes_under(&dest);
        let root_target = dest.join("tree");
        let nested_target = root_target.join("nested");
        let file_target = nested_target.join("b.txt");
        let stage = taken.first().unwrap().path.parent().unwrap();
        assert_eq!(
            stage.parent(),
            Some(dest.as_path()),
            "private staging directory"
        );
        let at = |final_path: &std::path::Path| {
            let path = stage.join(final_path.strip_prefix(&dest).unwrap());
            taken
                .iter()
                .find(|entry| entry.path == path)
                .unwrap_or_else(|| panic!("no creation record for {}", path.display()))
        };
        assert_eq!(taken.len(), 3, "unexpected creation records: {taken:?}");
        assert_eq!(at(&root_target).mode & 0o077, 0, "root not created private");
        assert_eq!(
            at(&nested_target).mode & 0o077,
            0,
            "nested dir not created private"
        );
        assert_eq!(at(&file_target).mode & 0o177, 0, "file not created private");
        assert_eq!(
            at(&file_target).len,
            0,
            "creation record must precede any written bytes"
        );
        // Final state still preserves the private source modes and content.
        let mode_of =
            |path: &std::path::Path| std::fs::metadata(path).expect("stat").mode() & 0o777;
        assert_eq!(mode_of(&root_target), 0o700);
        assert_eq!(mode_of(&nested_target), 0o700);
        assert_eq!(mode_of(&file_target), 0o600);
        assert_eq!(std::fs::read(&file_target).expect("read"), b"b");
    }

    #[test]
    fn copy_file_via_keys_is_private_before_first_write() {
        use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
        let src_tmp = tempfile::TempDir::new().expect("tempdir");
        let dst_tmp = tempfile::TempDir::new().expect("tempdir");
        let source = src_tmp.path().join("note.txt");
        std::fs::write(&source, "hello").expect("write");
        std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o600)).expect("chmod");

        let mut c = Controller::new(src_tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(source.clone()));

        // Keys: copy, go-to-folder with a typed path, paste.
        c.handle(key(K::Char('y'), M::empty()), 80, 24);
        let dest = dst_tmp.path().canonicalize().expect("canonicalize dest");
        go_to(&mut c, &dest);
        c.handle(key(K::Char('p'), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "paste must leave a pending mutation");
        settle(&mut c);

        // Creation-time evidence: private mode with no bytes yet, before the
        // copy and the final chmod.
        let taken = crate::actions::take_initial_modes_under(&dest);
        assert_eq!(taken.len(), 1, "unexpected creation records: {taken:?}");
        let stage = taken[0].path.parent().unwrap();
        assert_eq!(
            stage.parent(),
            Some(dest.as_path()),
            "private staging directory"
        );
        assert_eq!(taken[0].path, stage.join("note.txt"));
        assert_eq!(taken[0].mode & 0o177, 0, "file not created private");
        assert_eq!(
            taken[0].len, 0,
            "creation record must precede any written bytes"
        );
        // Final state still preserves the private source mode and content.
        let target = dest.join("note.txt");
        assert_eq!(
            std::fs::metadata(&target).expect("stat").mode() & 0o777,
            0o600
        );
        assert_eq!(std::fs::read(&target).expect("read"), b"hello");
    }

    #[test]
    fn buffered_enter_then_down_opens_original_directory() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        std::fs::create_dir(tmp.path().join("a-first")).expect("mkdir");
        std::fs::create_dir(tmp.path().join("b-second")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(tmp.path().join("a-first")));
        // Keys arrive buffered with no settle between them: Enter opens the
        // row selected at press time, Down then moves inside the new folder.
        c.handle(key(K::Enter, M::empty()), 80, 24);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        assert_eq!(c.ui.directory, tmp.path().join("a-first"));
    }

    #[test]
    fn cursor_move_before_enter_opens_moved_row() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        std::fs::create_dir(tmp.path().join("a-first")).expect("mkdir");
        std::fs::create_dir(tmp.path().join("b-second")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        assert_eq!(c.selected(), Some(tmp.path().join("a-first")));
        c.handle(key(K::Down, M::empty()), 80, 24);
        assert_eq!(c.selected(), Some(tmp.path().join("b-second")));
        c.handle(key(K::Enter, M::empty()), 80, 24);
        settle(&mut c);
        assert_eq!(c.ui.directory, tmp.path().join("b-second"));
    }

    #[test]
    fn parent_navigation_selects_previously_open_folder() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let child = tmp.path().join("child");
        std::fs::create_dir(&child).expect("mkdir");
        let mut c = Controller::new(child.clone());
        settle(&mut c);
        assert_eq!(c.ui.directory, child);
        c.handle(key(K::Char('h'), M::empty()), 80, 24);
        settle(&mut c);
        assert_eq!(c.ui.directory, tmp.path().to_path_buf());
        assert_eq!(c.selected(), Some(child));
    }

    #[test]
    fn go_to_dialog_navigates_to_real_directory() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let dest = tmp.path().join("destination");
        std::fs::create_dir(&dest).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        go_to(&mut c, &dest.canonicalize().expect("canonicalize dest"));
        assert_eq!(
            c.ui.directory,
            dest.canonicalize().expect("canonicalize dest")
        );
        assert!(
            c.ui.dialog.is_none(),
            "goto dialog must close after navigation"
        );
        assert!(!c.ui.loading);
    }

    #[test]
    fn go_to_invalid_path_keeps_directory_and_shows_error() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        let before = c.ui.directory.clone();
        c.handle(key(K::Char('g'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Name { .. })),
            "g must open goto dialog"
        );
        for ch in "no-such-dir-xyz".chars() {
            c.handle(key(K::Char(ch), M::empty()), 80, 24);
        }
        c.handle(key(K::Enter, M::empty()), 80, 24);
        assert_eq!(c.ui.directory, before, "invalid goto must not navigate");
        let (toast, severity) =
            c.ui.toast
                .clone()
                .expect("invalid goto must be user-visible");
        assert!(
            toast.contains("Not a directory"),
            "unexpected toast: {toast}"
        );
        assert!(matches!(severity, Severity::Error));
    }

    #[test]
    fn escape_cancels_delete_dialog_without_deleting() {
        let tmp = test_dir(&[("a.txt", "a"), ("b.txt", "b")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        let victim = tmp.path().join("a.txt");
        assert_eq!(c.selected(), Some(victim.clone()));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: true, .. })),
            "delete dialog must open"
        );
        c.handle(key(K::Esc, M::empty()), 80, 24);
        assert!(c.ui.dialog.is_none(), "escape must close the delete dialog");
        assert!(c.delete.is_none(), "escape must clear the pending target");
        assert!(
            c.mutation.is_none(),
            "cancelled delete must not start a mutation"
        );
        assert!(victim.exists(), "cancelled delete must keep the file");
        settle(&mut c);
        assert!(victim.exists(), "file must still exist after settle");
        assert!(tmp.path().join("b.txt").exists());
    }

    #[test]
    fn selection_changed_cancels_confirmed_delete() {
        let tmp = test_dir(&[("a.txt", "a"), ("b.txt", "b")]);
        std::fs::create_dir(tmp.path().join("sub")).expect("mkdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 80, 24);
        settle(&mut c);
        let victim = tmp.path().join("a.txt");
        let other = tmp.path().join("b.txt");
        assert_eq!(c.selected(), Some(victim.clone()));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: true, .. })),
            "delete dialog must open"
        );
        // Keys are modal-blocked while the dialog is open, so drive the guard
        // directly the way a list refresh would: the row now points elsewhere.
        c.ui.row += 1;
        assert_eq!(c.selected(), Some(other.clone()));
        c.handle(key(K::Tab, M::empty()), 80, 24);
        c.handle(key(K::Char(' '), M::empty()), 80, 24);
        assert!(
            c.mutation.is_none(),
            "stale confirm must not start a mutation"
        );
        let (toast, severity) = c.ui.toast.clone().expect("cancel must be user-visible");
        assert!(
            toast.contains("Selection changed"),
            "unexpected toast: {toast}"
        );
        assert!(matches!(severity, Severity::Warning));
        assert!(victim.exists(), "stale confirm must keep the original file");
        assert!(other.exists());
        settle(&mut c);
        assert!(victim.exists());
        assert!(other.exists());
    }

    #[test]
    fn folder_focus_blocks_copy_and_cut_via_keys() {
        for shortcut in ['x', 'y'] {
            let tmp = tempfile::TempDir::new().expect("tempdir");
            let current = tmp.path().join("current");
            std::fs::create_dir(&current).expect("mkdir");
            std::fs::create_dir(tmp.path().join("neighbour")).expect("mkdir");
            let target = current.join("important.txt");
            std::fs::write(&target, "keep").expect("write");
            let mut c = Controller::new(current.clone());
            settle(&mut c);
            assert_eq!(c.selected(), Some(target.clone()));
            c.handle(key(K::Char('b'), M::empty()), 130, 30);
            assert!(
                c.ui.focus == Focus::Folders,
                "b must focus folders at wide width"
            );
            c.handle(key(K::Char(shortcut), M::empty()), 130, 30);
            assert!(
                c.clip.is_none(),
                "{shortcut} from folder focus must not set clipboard"
            );
            assert!(
                c.mutation.is_none(),
                "{shortcut} from folder focus must not start a mutation"
            );
            assert!(
                c.ui.dialog.is_none(),
                "{shortcut} from folder focus must not open a dialog"
            );
            let (toast, severity) = c.ui.toast.clone().expect("warning must be user-visible");
            assert!(
                toast.contains("Focus the file list"),
                "unexpected toast for {shortcut}: {toast}"
            );
            assert!(matches!(severity, Severity::Warning));
            assert_eq!(
                std::fs::read(&target).expect("read"),
                b"keep",
                "{shortcut} from folder focus must leave the file alone"
            );
            settle(&mut c);
            assert_eq!(std::fs::read(&target).expect("read"), b"keep");
        }
    }

    #[test]
    fn folder_focus_blocks_delete_via_keys() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let current = tmp.path().join("current");
        std::fs::create_dir(&current).expect("mkdir");
        std::fs::create_dir(tmp.path().join("neighbour")).expect("mkdir");
        let target = current.join("important.txt");
        std::fs::write(&target, "keep").expect("write");
        let mut c = Controller::new(current.clone());
        settle(&mut c);
        assert_eq!(c.selected(), Some(target.clone()));
        c.handle(key(K::Char('b'), M::empty()), 130, 30);
        assert!(c.ui.focus == Focus::Folders);
        c.handle(key(K::Char('d'), M::empty()), 130, 30);
        assert!(
            c.ui.dialog.is_none(),
            "delete from folder focus must not open a confirm dialog"
        );
        assert!(
            c.delete.is_none(),
            "delete from folder focus must not stage a target"
        );
        assert!(
            c.mutation.is_none(),
            "delete from folder focus must not start a mutation"
        );
        let (toast, severity) = c.ui.toast.clone().expect("warning must be user-visible");
        assert!(
            toast.contains("Focus the file list"),
            "unexpected toast: {toast}"
        );
        assert!(matches!(severity, Severity::Warning));
        assert_eq!(std::fs::read(&target).expect("read"), b"keep");
        settle(&mut c);
        assert!(target.exists(), "blocked delete must keep the file");
    }

    #[test]
    fn folder_focus_blocks_other_file_actions_via_keys() {
        // `n` / `f`: blocked create dialogs leave no dialog and no new path.
        for shortcut in ['n', 'f'] {
            let tmp = tempfile::TempDir::new().expect("tempdir");
            let current = tmp.path().join("current");
            std::fs::create_dir(&current).expect("mkdir");
            std::fs::write(current.join("selected.txt"), "keep").expect("write");
            let mut c = Controller::new(current.clone());
            settle(&mut c);
            c.handle(key(K::Char('b'), M::empty()), 130, 30);
            assert!(c.ui.focus == Focus::Folders);
            c.handle(key(K::Char(shortcut), M::empty()), 130, 30);
            assert!(
                c.ui.dialog.is_none(),
                "{shortcut} from folder focus must not open a dialog"
            );
            assert!(c.mutation.is_none());
            let (toast, severity) = c.ui.toast.clone().expect("warning must be user-visible");
            assert!(
                toast.contains("Focus the file list"),
                "unexpected toast for {shortcut}: {toast}"
            );
            assert!(matches!(severity, Severity::Warning));
            assert_eq!(
                std::fs::read(current.join("selected.txt")).expect("read"),
                b"keep"
            );
            settle(&mut c);
            let names: Vec<_> = std::fs::read_dir(&current)
                .expect("readdir")
                .map(|e| e.expect("entry").file_name())
                .collect();
            assert_eq!(names.len(), 1, "blocked {shortcut} must not create a path");
        }

        // `r`: blocked rename leaves no dialog and the file untouched.
        {
            let tmp = tempfile::TempDir::new().expect("tempdir");
            let current = tmp.path().join("current");
            std::fs::create_dir(&current).expect("mkdir");
            let target = current.join("selected.txt");
            std::fs::write(&target, "keep").expect("write");
            let mut c = Controller::new(current.clone());
            settle(&mut c);
            c.handle(key(K::Char('b'), M::empty()), 130, 30);
            c.handle(key(K::Char('r'), M::empty()), 130, 30);
            assert!(
                c.ui.dialog.is_none(),
                "rename from folder focus must not open a dialog"
            );
            let (toast, severity) = c.ui.toast.clone().expect("warning must be user-visible");
            assert!(
                toast.contains("Focus the file list"),
                "unexpected toast: {toast}"
            );
            assert!(matches!(severity, Severity::Warning));
            assert_eq!(std::fs::read(&target).expect("read"), b"keep");
            settle(&mut c);
            assert!(target.exists());
        }

        // `e`: blocked edit returns no effect and leaves the file alone.
        {
            let tmp = tempfile::TempDir::new().expect("tempdir");
            let current = tmp.path().join("current");
            std::fs::create_dir(&current).expect("mkdir");
            let target = current.join("selected.txt");
            std::fs::write(&target, "keep").expect("write");
            let mut c = Controller::new(current.clone());
            settle(&mut c);
            c.handle(key(K::Char('b'), M::empty()), 130, 30);
            let effect = c.handle(key(K::Char('e'), M::empty()), 130, 30);
            assert!(
                matches!(effect, Effect::None),
                "edit from folder focus must not launch an editor"
            );
            assert!(c.ui.dialog.is_none());
            let (toast, severity) = c.ui.toast.clone().expect("warning must be user-visible");
            assert!(
                toast.contains("Focus the file list"),
                "unexpected toast: {toast}"
            );
            assert!(matches!(severity, Severity::Warning));
            assert_eq!(std::fs::read(&target).expect("read"), b"keep");
        }

        // `p`: blocked paste with a staged clipboard starts no mutation.
        {
            let tmp = tempfile::TempDir::new().expect("tempdir");
            let current = tmp.path().join("current");
            std::fs::create_dir(&current).expect("mkdir");
            std::fs::write(current.join("selected.txt"), "keep").expect("write");
            let source = tmp.path().join("source.txt");
            std::fs::write(&source, "keep").expect("write");
            let mut c = Controller::new(current.clone());
            settle(&mut c);
            c.clip = Some((source.clone(), false));
            c.handle(key(K::Char('b'), M::empty()), 130, 30);
            assert!(c.ui.focus == Focus::Folders);
            c.handle(key(K::Char('p'), M::empty()), 130, 30);
            assert!(
                c.mutation.is_none(),
                "paste from folder focus must not start a mutation"
            );
            assert_eq!(c.clip, Some((source.clone(), false)));
            assert!(
                c.ui.dialog.is_none(),
                "paste from folder focus must not open a dialog"
            );
            let (toast, severity) = c.ui.toast.clone().expect("warning must be user-visible");
            assert!(
                toast.contains("Focus the file list"),
                "unexpected toast: {toast}"
            );
            assert!(matches!(severity, Severity::Warning));
            assert!(
                !current.join("source.txt").exists(),
                "blocked paste must not copy the file"
            );
            assert_eq!(std::fs::read(&source).expect("read"), b"keep");
            settle(&mut c);
            assert!(!current.join("source.txt").exists());
        }
    }

    #[test]
    fn resize_preserves_selection_when_folder_column_hides() {
        let tmp = test_dir(&[("a.txt", "a"), ("b.txt", "b")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(key(K::Down, M::empty()), 130, 30);
        settle(&mut c);
        let before = tmp.path().join("b.txt");
        assert_eq!(c.selected(), Some(before.clone()));
        c.handle(key(K::Char('b'), M::empty()), 130, 30);
        assert!(c.ui.focus == Focus::Folders);
        let focused_selection = c.selected().expect("selection under folder focus");
        assert_eq!(focused_selection, before);
        c.handle(Event::Resize(80, 24), 80, 24);
        assert!(
            c.ui.focus == Focus::Table,
            "hiding the folder column must fall back to the file list"
        );
        assert_eq!(c.selected(), Some(before.clone()));
        assert_eq!(c.ui.listing.entries.len(), 2);
        c.handle(Event::Resize(130, 30), 130, 30);
        assert_eq!(c.selected(), Some(before.clone()));
        assert_eq!(c.ui.listing.entries.len(), 2);
        settle(&mut c);
        assert_eq!(c.selected(), Some(before));
    }

    #[test]
    fn preview_focus_remains_reachable_after_resize_round_trip() {
        let tmp = test_dir(&[("notes.txt", "hello")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        c.handle(Event::Resize(50, 24), 50, 24);
        c.handle(key(K::Char('v'), M::empty()), 50, 24);
        assert!(c.ui.focus == Focus::Preview);
        c.handle(Event::Resize(130, 30), 130, 30);
        c.handle(key(K::Char('v'), M::empty()), 130, 30);
        assert!(c.ui.focus == Focus::Table);
        c.handle(Event::Resize(50, 24), 50, 24);
        assert!(c.ui.focus == Focus::Preview);
        c.handle(key(K::Char('v'), M::empty()), 50, 24);
        assert!(c.ui.focus == Focus::Table);
        assert_eq!(c.selected(), Some(tmp.path().join("notes.txt")));
    }

    #[test]
    fn delete_during_slow_scan_becomes_ready_with_selected_name() {
        let tmp = test_dir(&[("remove.txt", "x")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        // Hold the initial scan on a test-controlled channel instead of
        // sleeping: the real worker reply is discarded with its receiver.
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        assert!(c.ui.loading);
        assert!(c.ui.listing.entries.is_empty());
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: false, .. })),
            "delete during slow scan must show a pending confirmation"
        );
        if let Some(Dialog::Delete { name, .. }) = &c.ui.dialog {
            assert!(
                name.contains("Loading"),
                "pending detail must say loading, got {name:?}"
            );
        } else {
            panic!("delete dialog missing");
        }
        assert!(c.delete.is_none());
        assert!(c.mutation.is_none());
        // Complete the held scan with the real listing; no sleeps, the
        // channel rendezvous is the only synchronization.
        let listing = crate::data::list(tmp.path(), false);
        hold_tx
            .send(Reply::List(listing, None))
            .expect("send held listing");
        settle(&mut c);
        let target = tmp.path().join("remove.txt");
        assert_eq!(c.selected(), Some(target.clone()));
        assert_eq!(c.delete.as_ref().map(|v| v.0.clone()), Some(target.clone()));
        if let Some(Dialog::Delete { name, ready, .. }) = &c.ui.dialog {
            assert!(ready, "scan completion must enable the delete button");
            assert_eq!(name, "remove.txt");
        } else {
            panic!("delete dialog must stay open after scan");
        }
        c.handle(key(K::Tab, M::empty()), 80, 24);
        c.handle(key(K::Char(' '), M::empty()), 80, 24);
        assert!(c.mutation.is_some(), "ready confirm must start a mutation");
        settle(&mut c);
        assert!(!target.exists(), "confirmed delete must remove the file");
    }

    #[test]
    fn escape_cancels_pending_delete_without_waiting_for_scan() {
        let tmp = test_dir(&[("keep.txt", "keep")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: false, .. })),
            "slow-scan delete must be pending"
        );
        c.handle(key(K::Esc, M::empty()), 80, 24);
        assert!(c.ui.dialog.is_none(), "escape must close the dialog");
        assert!(c.delete.is_none(), "escape must clear the pending target");
        assert!(c.mutation.is_none(), "escape must not start a mutation");
        assert!(
            c.list_worker.is_some(),
            "escape must not wait for the slow scan"
        );
        assert!(tmp.path().join("keep.txt").exists());
        let listing = crate::data::list(tmp.path(), false);
        hold_tx
            .send(Reply::List(listing, None))
            .expect("send held listing");
        settle(&mut c);
        assert!(c.ui.dialog.is_none());
        assert!(c.delete.is_none());
        assert!(c.mutation.is_none());
        assert!(tmp.path().join("keep.txt").exists());
    }

    #[test]
    fn mouse_cancels_pending_delete_without_waiting_for_scan() {
        let tmp = test_dir(&[("keep.txt", "keep")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(matches!(
            c.ui.dialog,
            Some(Dialog::Delete { ready: false, .. })
        ));
        let cancel = (0..24)
            .flat_map(|y| (0..80).map(move |x| (x, y)))
            .find(|&(x, y)| matches!(ui::hit(&c.ui, 80, 24, x, y), Hit::Cancel))
            .expect("cancel button");
        c.handle(
            mouse(Mouse::Down(MouseButton::Left), cancel.0, cancel.1),
            80,
            24,
        );
        assert!(c.ui.dialog.is_none());
        assert!(c.list_worker.is_some());
        assert!(c.mutation.is_none());
        assert_eq!(std::fs::read(tmp.path().join("keep.txt")).unwrap(), b"keep");
        hold_tx
            .send(Reply::List(crate::data::list(tmp.path(), false), None))
            .unwrap();
        settle(&mut c);
        assert_eq!(std::fs::read(tmp.path().join("keep.txt")).unwrap(), b"keep");
    }

    #[test]
    fn pending_delete_blocks_navigation_until_scan_completes() {
        let tmp = tempfile::tempdir().unwrap();
        let old = tmp.path().join("old");
        let new = tmp.path().join("new");
        std::fs::create_dir(&old).unwrap();
        std::fs::create_dir(&new).unwrap();
        std::fs::write(old.join("old.txt"), "old").unwrap();
        std::fs::write(new.join("new.txt"), "new").unwrap();
        let mut c = Controller::new(old.clone());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(matches!(
            c.ui.dialog,
            Some(Dialog::Delete { ready: false, .. })
        ));
        c.handle(key(K::Char('h'), M::empty()), 80, 24);
        c.handle(key(K::Char('g'), M::empty()), 80, 24);
        assert_eq!(c.ui.directory, old);
        assert!(c.delete.is_none());
        hold_tx
            .send(Reply::List(crate::data::list(&old, false), None))
            .unwrap();
        settle(&mut c);
        assert_eq!(
            c.delete.as_ref().map(|v| v.0.clone()),
            Some(old.join("old.txt"))
        );
        assert_eq!(std::fs::read(new.join("new.txt")).unwrap(), b"new");
    }

    #[test]
    fn late_listing_cannot_replace_directory_opened_during_scan() {
        let tmp = tempfile::tempdir().unwrap();
        let old = tmp.path().join("old");
        let new = tmp.path().join("new");
        std::fs::create_dir(&old).unwrap();
        std::fs::create_dir(&new).unwrap();
        std::fs::write(old.join("old.txt"), "old").unwrap();
        std::fs::write(new.join("new.txt"), "new").unwrap();
        let mut c = Controller::new(old.clone());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('g'), M::empty()), 80, 24);
        for ch in new.to_str().unwrap().chars() {
            c.handle(key(K::Char(ch), M::empty()), 80, 24);
        }
        c.handle(key(K::Enter, M::empty()), 80, 24);
        assert_eq!(c.ui.directory, new);
        hold_tx
            .send(Reply::List(crate::data::list(&old, false), None))
            .unwrap();
        settle(&mut c);
        assert_eq!(c.ui.directory, new);
        assert_eq!(c.selected(), Some(new.join("new.txt")));
        assert_eq!(c.ui.listing.entries.len(), 1);
    }

    #[test]
    fn confirmed_delete_after_navigation_cancels_without_deleting() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let old = tmp.path().join("old");
        let new = tmp.path().join("new");
        std::fs::create_dir(&old).expect("mkdir");
        std::fs::create_dir(&new).expect("mkdir");
        let old_file = old.join("old.txt");
        let new_file = new.join("new.txt");
        std::fs::write(&old_file, "old").expect("write");
        std::fs::write(&new_file, "new").expect("write");
        let mut c = Controller::new(old.clone());
        settle(&mut c);
        assert_eq!(c.selected(), Some(old_file.clone()));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: true, .. })),
            "delete dialog must open when ready"
        );
        // Navigate to the newer directory; the dialog stays open but the
        // captured target must go stale.
        c.ui.directory = new.clone();
        c.reload(None);
        settle(&mut c);
        assert_eq!(c.ui.directory, new);
        assert_eq!(c.selected(), Some(new_file.clone()));
        assert_eq!(
            c.delete.as_ref().map(|v| v.0.clone()),
            Some(old_file.clone())
        );
        c.handle(key(K::Tab, M::empty()), 80, 24);
        c.handle(key(K::Char(' '), M::empty()), 80, 24);
        assert!(
            c.mutation.is_none(),
            "stale confirm after navigation must not start a mutation"
        );
        let (toast, severity) = c.ui.toast.clone().expect("cancel must be visible");
        assert!(
            toast.contains("Selection changed"),
            "unexpected toast: {toast}"
        );
        assert!(matches!(severity, Severity::Warning));
        assert!(old_file.exists(), "stale confirm must keep the old file");
        assert!(new_file.exists(), "stale confirm must keep the new file");
        settle(&mut c);
        assert!(old_file.exists());
        assert!(new_file.exists());
    }

    #[test]
    fn empty_folder_pending_delete_never_enables() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let mut c = Controller::new(tmp.path().to_path_buf());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: false, .. })),
            "empty folder must show pending delete"
        );
        assert!(c.delete.is_none());
        let listing = crate::data::list(tmp.path(), false);
        assert!(listing.entries.is_empty());
        hold_tx
            .send(Reply::List(listing, None))
            .expect("send held listing");
        settle(&mut c);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: false, .. })),
            "empty folder must never enable delete"
        );
        assert!(c.delete.is_none());
        assert!(c.mutation.is_none());
        if let Some(Dialog::Delete { name, .. }) = &c.ui.dialog {
            assert!(
                name.contains("No current selection"),
                "empty detail must say no selection, got {name:?}"
            );
        } else {
            panic!("delete dialog missing");
        }
        c.handle(key(K::Esc, M::empty()), 80, 24);
        assert!(c.ui.dialog.is_none());
    }

    #[test]
    fn empty_listing_finishes_loading_when_restore_target_is_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let mut c = Controller::new(tmp.path().to_path_buf());
        c.reload(Some(tmp.path().join("already-gone.txt")));
        settle(&mut c);
        assert!(!c.ui.loading);
        assert_eq!(c.selected(), None);
        assert!(c.ui.listing.entries.is_empty());
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(c.ui.dialog.is_none());
        assert!(c.delete.is_none());
    }

    #[test]
    fn cut_during_slow_scan_returns_without_waiting() {
        let tmp = test_dir(&[("keep.txt", "keep")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('x'), M::empty()), 80, 24);
        assert!(
            c.clip.is_none(),
            "cut during slow scan must not set clipboard"
        );
        assert!(c.mutation.is_none());
        assert!(
            c.list_worker.is_some(),
            "cut must not wait for the slow scan"
        );
        let (toast, severity) = c.ui.toast.clone().expect("warning must be visible");
        assert!(toast.contains("still loading"), "unexpected toast: {toast}");
        assert!(matches!(severity, Severity::Warning));
        assert!(tmp.path().join("keep.txt").exists());
        let listing = crate::data::list(tmp.path(), false);
        hold_tx
            .send(Reply::List(listing, None))
            .expect("send held listing");
        settle(&mut c);
        assert!(c.clip.is_none());
        assert!(tmp.path().join("keep.txt").exists());
    }

    #[test]
    fn quit_during_pending_delete_does_not_wait_for_scan() {
        let tmp = test_dir(&[("keep.txt", "keep")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        c.handle(key(K::Char('d'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Delete { ready: false, .. })),
            "slow-scan delete must be pending"
        );
        assert!(
            matches!(
                c.handle(key(K::Char('q'), M::CONTROL), 80, 24),
                Effect::Quit
            ),
            "quit during pending delete must quit immediately"
        );
        assert!(
            c.list_worker.is_some(),
            "quit must not wait for the slow scan"
        );
        assert!(tmp.path().join("keep.txt").exists());
        let listing = crate::data::list(tmp.path(), false);
        hold_tx
            .send(Reply::List(listing, None))
            .expect("send held listing");
        settle(&mut c);
        assert!(tmp.path().join("keep.txt").exists());
    }

    #[test]
    fn slow_listing_keeps_create_dialog_responsive() {
        let tmp = test_dir(&[("keep.txt", "keep")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        let (hold_tx, hold_rx) = mpsc::channel::<Reply>();
        c.list_worker = Some((c.list_rev, hold_rx));
        assert!(c.ui.loading);
        c.handle(key(K::Char('n'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Name { .. })),
            "n during slow scan must open create dialog"
        );
        c.handle(key(K::Char('x'), M::empty()), 80, 24);
        if let Some(Dialog::Name { value, .. }) = &c.ui.dialog {
            assert!(
                value.contains('x'),
                "typed char must appear while scan pending, got {value:?}"
            );
        } else {
            panic!("create dialog missing");
        }
        c.handle(key(K::Esc, M::empty()), 80, 24);
        assert!(c.ui.dialog.is_none(), "escape must close create dialog");
        assert!(c.list_worker.is_some(), "escape must not wait for scan");
        assert!(c.mutation.is_none());
        let listing = crate::data::list(tmp.path(), false);
        hold_tx
            .send(Reply::List(listing, None))
            .expect("send held listing");
        settle(&mut c);
        assert!(!c.ui.loading);
        assert_eq!(c.ui.listing.entries.len(), 1);
        c.handle(key(K::Char('n'), M::empty()), 80, 24);
        for ch in "newdir".chars() {
            c.handle(key(K::Char(ch), M::empty()), 80, 24);
        }
        c.handle(key(K::Enter, M::empty()), 80, 24);
        settle(&mut c);
        assert!(tmp.path().join("newdir").is_dir());
    }

    #[test]
    fn keyboard_rename_changes_real_file() {
        let tmp = test_dir(&[("before.txt", "unchanged content")]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        let source = tmp.path().join("before.txt");
        assert_eq!(c.selected(), Some(source.clone()));
        c.handle(key(K::Char('r'), M::empty()), 80, 24);
        assert!(
            matches!(c.ui.dialog, Some(Dialog::Name { .. })),
            "r must open rename dialog"
        );
        for ch in "after.txt".chars() {
            c.handle(key(K::Char(ch), M::empty()), 80, 24);
        }
        c.handle(key(K::Enter, M::empty()), 80, 24);
        settle(&mut c);
        let target = tmp.path().join("after.txt");
        assert_eq!(
            std::fs::read(&target).expect("renamed file"),
            b"unchanged content"
        );
        assert!(!source.exists(), "rename must move the real file");
        assert!(c.ui.dialog.is_none());
        assert_eq!(c.selected(), Some(target));
    }

    #[test]
    fn keyboard_horizontal_preview_scroll() {
        let body = format!("first {} last", "x".repeat(120));
        let tmp = test_dir(&[("long.txt", &body)]);
        let mut c = Controller::new(tmp.path().to_path_buf());
        settle(&mut c);
        assert!(c.ui.preview.is_some());
        assert_eq!(c.ui.preview_x, 0);
        c.handle(key(K::Char('v'), M::empty()), 80, 24);
        assert!(c.ui.focus == Focus::Preview, "v must focus preview");
        c.handle(key(K::Right, M::empty()), 80, 24);
        c.handle(key(K::Right, M::empty()), 80, 24);
        c.handle(key(K::Right, M::empty()), 80, 24);
        assert!(
            c.ui.preview_x > 0,
            "right in preview must scroll horizontally, got {}",
            c.ui.preview_x
        );
        assert!(c.ui.focus == Focus::Preview, "focus must stay in preview");
    }

    #[test]
    fn folder_shortcut_focuses_folders() {
        let tmp = tempfile::TempDir::new().expect("tempdir");
        let current = tmp.path().join("current");
        std::fs::create_dir(&current).expect("mkdir");
        std::fs::create_dir(tmp.path().join("neighbour")).expect("mkdir");
        std::fs::write(current.join("a.txt"), "a").expect("write");
        let mut c = Controller::new(current.clone());
        settle(&mut c);
        assert!(c.ui.focus == Focus::Table, "starts in file list");
        c.handle(key(K::Char('b'), M::empty()), 130, 30);
        assert!(c.ui.focus == Focus::Folders, "b must focus folders");
        settle(&mut c);
        assert!(c.ui.focus == Focus::Folders, "folder focus must persist");
    }

    #[test]
    fn keyboard_copy_and_cut_paste_real_file() {
        for moving in [false, true] {
            let tmp = tempfile::TempDir::new().expect("tempdir");
            let source_dir = tmp.path().join("source");
            let destination = tmp.path().join("destination");
            std::fs::create_dir(&source_dir).expect("mkdir");
            std::fs::create_dir(&destination).expect("mkdir");
            std::fs::write(source_dir.join("notes.txt"), "actual content").expect("write");
            let mut c = Controller::new(source_dir.clone());
            settle(&mut c);
            assert_eq!(c.selected(), Some(source_dir.join("notes.txt")));
            c.handle(
                key(K::Char(if moving { 'x' } else { 'y' }), M::empty()),
                80,
                24,
            );
            assert!(c.clip.is_some());
            c.handle(key(K::Char('h'), M::empty()), 80, 24);
            settle(&mut c);
            assert_eq!(c.ui.directory, tmp.path().to_path_buf());
            let idx =
                c.ui.listing
                    .entries
                    .iter()
                    .position(|e| e.path == destination)
                    .expect("destination row");
            while c.selected().as_ref() != Some(&destination) {
                let cur = c.ui.row;
                if cur < idx {
                    c.handle(key(K::Down, M::empty()), 80, 24);
                } else {
                    c.handle(key(K::Up, M::empty()), 80, 24);
                }
            }
            c.handle(key(K::Enter, M::empty()), 80, 24);
            settle(&mut c);
            assert_eq!(c.ui.directory, destination);
            c.handle(key(K::Char('p'), M::empty()), 80, 24);
            assert!(c.mutation.is_some(), "paste must start a mutation");
            settle(&mut c);
            assert_eq!(
                std::fs::read(destination.join("notes.txt")).expect("pasted"),
                b"actual content"
            );
            assert_eq!(
                source_dir.join("notes.txt").exists(),
                !moving,
                "cut must move, copy must keep"
            );
        }
    }
}
