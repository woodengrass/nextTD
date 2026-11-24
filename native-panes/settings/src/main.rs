mod app;
mod input;
mod preferences;
mod terminal;
mod ui;

use crossterm::event;
use std::time::Duration;

fn main() -> anyhow::Result<()> {
    let mut app = app::App::new()?;
    terminal::with_screen(|screen| {
        loop {
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
