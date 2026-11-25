mod app;
mod input;
mod preferences;
mod terminal;
mod ui;

use crossterm::event;
use std::time::Duration;

fn main() -> anyhow::Result<()> {
    let mut app = app::App::new()?;
    use signal_hook::{
        consts::{SIGHUP, SIGTERM},
        flag,
    };
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };
    let stopping = Arc::new(AtomicBool::new(false));
    for signal in [SIGTERM, SIGHUP] {
        flag::register(signal, Arc::clone(&stopping))?;
    }
    terminal::with_screen(|screen| {
        while !stopping.load(Ordering::Relaxed) {
            app.poll();
            let mut targets = ui::Targets(Vec::new());
            screen.draw(|frame| targets = ui::draw(frame, &mut app))?;
            if event::poll(Duration::from_millis(100))?
                && input::handle(&mut app, event::read()?, &targets)
            {
                break;
            }
        }
        Ok(())
    })
}
