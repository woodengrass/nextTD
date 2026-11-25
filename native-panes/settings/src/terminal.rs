use std::io::{self, Stdout, Write};

use crossterm::{
    cursor::Show,
    event::{DisableFocusChange, DisableMouseCapture, EnableFocusChange, EnableMouseCapture},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, backend::CrosstermBackend};

pub type Screen = Terminal<CrosstermBackend<AnsiOutput>>;

pub struct AnsiOutput {
    stdout: Stdout,
    buffer: Vec<u8>,
}

impl Write for AnsiOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.buffer.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        let mut cursor = 0;
        while cursor < self.buffer.len() {
            let remaining = &self.buffer[cursor..];
            if remaining.starts_with(b"\x1b[")
                && let Some(end) = remaining[2..]
                    .iter()
                    .position(|byte| !(byte.is_ascii_digit() || *byte == b';'))
                && remaining[2 + end] == b'm'
            {
                let params = &remaining[2..2 + end];
                self.stdout.write_all(b"\x1b[")?;
                let mut parts = params.split(|byte| *byte == b';').peekable();
                let mut separator = false;
                while let Some(part) = parts.next() {
                    if separator {
                        self.stdout.write_all(b";")?;
                    }
                    separator = true;
                    if (part == b"38" || part == b"48") && parts.peek() == Some(&b"5".as_slice()) {
                        let mut lookahead = parts.clone();
                        lookahead.next();
                        if let Some(value) = lookahead.next()
                            && let Ok(number) = std::str::from_utf8(value)
                            && let Ok(color) = number.parse::<u8>()
                            && color < 16
                        {
                            parts.next();
                            parts.next();
                            let base = if part == b"38" { 30 } else { 40 };
                            let code = base + if color < 8 { color } else { color + 52 };
                            write!(self.stdout, "{code}")?;
                            continue;
                        }
                    }
                    self.stdout.write_all(part)?;
                }
                self.stdout.write_all(b"m")?;
                cursor += 3 + end;
            } else {
                self.stdout.write_all(&remaining[..1])?;
                cursor += 1;
            }
        }
        self.buffer.clear();
        self.stdout.flush()
    }
}

struct Session;

impl Drop for Session {
    fn drop(&mut self) {
        if let Err(error) = execute!(
            io::stdout(),
            DisableFocusChange,
            DisableMouseCapture,
            Show,
            LeaveAlternateScreen
        ) {
            eprintln!("Cannot restore terminal screen: {error}");
        }
        if let Err(error) = disable_raw_mode() {
            eprintln!("Cannot restore terminal mode: {error}");
        }
    }
}

pub fn with_screen(run: impl FnOnce(&mut Screen) -> anyhow::Result<()>) -> anyhow::Result<()> {
    enable_raw_mode()?;
    let _session = Session;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableFocusChange
    )?;
    run(&mut Terminal::new(CrosstermBackend::new(AnsiOutput {
        stdout: io::stdout(),
        buffer: Vec::new(),
    }))?)
}
