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
    let input_closed = Arc::new(AtomicBool::new(false));
    let result = terminal::with_screen(|screen| {
        use std::sync::mpsc::{RecvTimeoutError, sync_channel};
        let (tx, rx) = sync_channel(1);
        let closed = Arc::clone(&input_closed);
        // A disconnected tty can trap crossterm inside read(). Keep terminal
        // restoration on the main thread, independent of the input decoder.
        std::thread::spawn(move || {
            loop {
                let event = event::read();
                if event.as_ref().is_err_and(terminal::is_terminal_disconnect) {
                    closed.store(true, Ordering::Release);
                }
                let failed = event.is_err();
                if tx.send(event).is_err() || failed {
                    break;
                }
            }
        });
        let mut back = None;
        while !stopping.load(Ordering::Relaxed) && !input_closed.load(Ordering::Acquire) {
            if terminal::hung_up()? {
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
    });
    // The tty can disappear after poll(), while Terminal::new or draw()
    // queries its size. Only classify terminal I/O failures as a clean hangup.
    match result {
        Err(error)
            if error.downcast_ref::<std::io::Error>().is_some()
                && (stopping.load(Ordering::Relaxed)
                    || input_closed.load(Ordering::Acquire)
                    || terminal::hung_up()?) =>
        {
            Ok(())
        }
        other => other,
    }
}
