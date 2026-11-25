use anyhow::{Context, Result};
use rustix::process::{Pid, Signal, kill_process_group};
use std::{
    fs::File,
    io::{Read, Seek},
    os::unix::process::CommandExt,
    process::{Child, Command, Stdio},
};

pub struct Back {
    child: Child,
    stderr: File,
}

impl Back {
    pub fn start() -> Result<Self> {
        let binary = std::env::var_os("TDNEXT_ZELLIJ").context("TDNEXT_ZELLIJ is not set")?;
        let stderr = tempfile::tempfile()?;
        let child = Command::new(binary)
            .args(["action", "return-to-origin"])
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(stderr.try_clone()?)
            .spawn()?;
        Ok(Self { child, stderr })
    }

    pub fn finished(&mut self) -> Result<bool> {
        let Some(status) = self.child.try_wait()? else {
            return Ok(false);
        };
        if !status.success() {
            self.stderr.rewind()?;
            let mut message = Vec::new();
            self.stderr.read_to_end(&mut message)?;
            anyhow::bail!("{status}: {}", String::from_utf8_lossy(&message));
        }
        Ok(true)
    }
}

impl Drop for Back {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            let _ = kill_process_group(Pid::from_child(&self.child), Signal::KILL);
            let _ = self.child.wait();
        }
    }
}
