mod actions;
#[cfg(test)]
mod actions_tests;
mod ansi_output;
mod controller;
mod data;
mod desktop_ui;
mod terminal;
mod ui;

use controller::{Controller, Effect};
use crossterm::event;
use std::{env, io, path::PathBuf};

fn main() -> io::Result<()> {
    let dir = env::args_os()
        .nth(1)
        .or_else(|| env::var_os("TDNEXT_DIRECTORY"));
    let dir = dir.map_or_else(env::current_dir, |dir| PathBuf::from(dir).canonicalize())?;
    let mut app = Controller::new(dir);
    let closing = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    for signal in [libc::SIGTERM, libc::SIGHUP] {
        signal_hook::flag::register(signal, closing.clone())?;
    }
    terminal::with_screen(|screen| {
        'events: loop {
            app.poll();
            screen.draw(|frame| ui::draw(frame, &app.ui))?;
            let deadline = app.timeout().map(|t| std::time::Instant::now() + t);
            loop {
                if closing.load(std::sync::atomic::Ordering::Relaxed) {
                    break 'events;
                }
                let tick = std::time::Duration::from_millis(100);
                let wait = deadline.map_or(tick, |d| {
                    tick.min(d.saturating_duration_since(std::time::Instant::now()))
                });
                if event::poll(wait)? {
                    break;
                }
                if deadline.is_some_and(|d| std::time::Instant::now() >= d) {
                    continue 'events;
                }
            }
            let size = screen.size()?;
            match app.handle(event::read()?, size.width, size.height) {
                Effect::None => {}
                Effect::Quit => break,
                Effect::Edit(mut command, path) => {
                    let status = terminal::edit(screen, &mut command)?;
                    app.after_edit(path, status);
                }
            }
        }
        Ok(())
    })
}
