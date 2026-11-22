use std::io::{self, Stdout};
use std::process::{Command, ExitStatus};

use crossterm::{
    cursor::Show,
    event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, backend::CrosstermBackend};

pub type Screen = Terminal<CrosstermBackend<Stdout>>;

fn restore() -> io::Result<()> {
    let screen = execute!(
        io::stdout(),
        DisableMouseCapture,
        DisableBracketedPaste,
        Show,
        LeaveAlternateScreen
    );
    screen.and(disable_raw_mode())
}

struct Session;

impl Drop for Session {
    fn drop(&mut self) {
        if let Err(error) = restore() {
            eprintln!("Cannot restore terminal: {error}");
        }
    }
}

fn enter() -> io::Result<()> {
    enable_raw_mode()?;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture
    )
}

pub fn with_screen(run: impl FnOnce(&mut Screen) -> io::Result<()>) -> io::Result<()> {
    let _session = Session;
    enter()?;
    run(&mut Terminal::new(CrosstermBackend::new(io::stdout()))?)
}

pub fn edit(screen: &mut Screen, command: &mut Command) -> io::Result<io::Result<ExitStatus>> {
    restore()?;
    let status = command.status();
    enter()?;
    screen.clear()?;
    Ok(status)
}
