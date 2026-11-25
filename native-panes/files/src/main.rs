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
    terminal::with_screen(|screen| {
        loop {
            app.poll();
            screen.draw(|frame| {
                let size = frame.area();
                app.resize(size.width, size.height);
                ui::draw(frame, &app.ui);
            })?;
            if let Some(timeout) = app.timeout()
                && !event::poll(timeout)?
            {
                continue;
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
