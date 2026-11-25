mod app;
mod back;
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
        use rustix::event::{PollFd, PollFlags, Timespec, poll};
        use std::sync::mpsc::{RecvTimeoutError, sync_channel};
        let (tx, rx) = sync_channel(1);
        // A disconnected tty can trap crossterm inside read(). Keep terminal
        // restoration on the main thread, independent of the input decoder.
        std::thread::spawn(move || {
            loop {
                let event = event::read();
                let failed = event.is_err();
                if tx.send(event).is_err() || failed {
                    break;
                }
            }
        });
        let stdin = std::io::stdin();
        let mut tty = [PollFd::new(&stdin, PollFlags::empty())];
        let mut back = None;
        while !stopping.load(Ordering::Relaxed) {
            if poll(&mut tty, Some(&Timespec::default()))? != 0 {
                break;
            }
            app.poll(&mut back);
            let mut targets = ui::Targets(Vec::new());
            screen.draw(|frame| targets = ui::draw(frame, &mut app))?;
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok(event) => {
                    if input::handle(&mut app, event?, &targets) {
                        break;
                    }
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => break,
            }
        }
        Ok(())
    })
}
