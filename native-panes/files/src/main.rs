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
use std::{env, io, path::PathBuf, sync::mpsc};

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
    terminal::with_screen(|screen, tty| {
        // crossterm can loop inside read on a hung-up PTY. Keep the UI thread
        // interruptible, and request only one event so an editor owns its input.
        let (request, requests) = mpsc::channel();
        let (send, events) = mpsc::channel();
        std::thread::spawn(move || {
            while requests.recv().is_ok() {
                if send.send(event::read()).is_err() {
                    break;
                }
            }
        });
        let request_event = || {
            request
                .send(())
                .map_err(|_| io::Error::other("Files input reader stopped"))
        };
        request_event()?;
        'events: loop {
            if closing.load(std::sync::atomic::Ordering::Relaxed) || terminal::hung_up(tty)? {
                break;
            }
            app.poll();
            screen.draw(|frame| {
                let size = frame.area();
                app.resize(size.width, size.height);
                ui::draw(frame, &app.ui);
            })?;
            let deadline = app.timeout().map(|t| std::time::Instant::now() + t);
            let input = loop {
                if closing.load(std::sync::atomic::Ordering::Relaxed) || terminal::hung_up(tty)? {
                    break 'events;
                }
                let tick = std::time::Duration::from_millis(100);
                let wait = deadline.map_or(tick, |d| {
                    tick.min(d.saturating_duration_since(std::time::Instant::now()))
                });
                match events.recv_timeout(wait) {
                    Ok(input) => break input?,
                    Err(mpsc::RecvTimeoutError::Timeout) => (),
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        return Err(io::Error::other("Files input reader stopped"));
                    }
                }
                if deadline.is_some_and(|d| std::time::Instant::now() >= d) {
                    continue 'events;
                }
            };
            let size = screen.size()?;
            match app.handle(input, size.width, size.height) {
                Effect::None => {}
                Effect::Quit => break,
                Effect::Edit(mut command, path) => {
                    let status = terminal::edit(screen, &mut command)?;
                    app.after_edit(path, status);
                }
            }
            request_event()?;
        }
        Ok(())
    })
}
