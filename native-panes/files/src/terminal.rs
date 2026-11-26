use std::io::{self, IsTerminal, Stdout, Write as _};
use std::os::fd::{AsFd, OwnedFd};
use std::process::{Command, ExitStatus};

use crossterm::{
    cursor::Show,
    event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, backend::CrosstermBackend};

pub type Screen = Terminal<CrosstermBackend<crate::ansi_output::AnsiOutput<Stdout>>>;

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
            let _ = writeln!(io::stderr(), "Cannot restore terminal: {error}");
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

pub fn hung_up(tty: &impl AsFd) -> io::Result<bool> {
    use rustix::event::{PollFd, PollFlags, Timespec, poll};
    let mut fd = [PollFd::new(tty, PollFlags::empty())];
    match poll(&mut fd, Some(&Timespec::default())) {
        Ok(_) => Ok(fd[0]
            .revents()
            .intersects(PollFlags::HUP | PollFlags::ERR | PollFlags::NVAL)),
        Err(rustix::io::Errno::INTR) => Ok(false),
        Err(e) => Err(e.into()),
    }
}

pub fn with_screen(run: impl FnOnce(&mut Screen, &OwnedFd) -> io::Result<()>) -> io::Result<()> {
    let tty = if io::stdin().is_terminal() {
        io::stdin().as_fd().try_clone_to_owned()?
    } else {
        std::fs::File::open("/dev/tty")?.into()
    };
    let _session = Session;
    enter()?;
    let desktop =
        std::env::var_os("TDNEXT_ZELLIJ").is_some() && std::env::var_os("ZELLIJ_PANE_ID").is_some();
    let output = crate::ansi_output::AnsiOutput::new(io::stdout(), desktop);
    let mut screen = Terminal::new(CrosstermBackend::new(output))?;
    let result = run(&mut screen, &tty);
    if hung_up(&tty)? {
        // The PTY is gone. Ratatui's Drop writes to stdout and reports its
        // failure on stderr, which can be the same dead PTY and panic.
        std::process::exit(0);
    }
    result
}

pub fn edit(screen: &mut Screen, command: &mut Command) -> io::Result<io::Result<ExitStatus>> {
    restore()?;
    let status = command.status();
    enter()?;
    screen.clear()?;
    Ok(status)
}
