use std::io::{self, Write};

pub struct AnsiOutput<W> {
    inner: W,
    pending: Vec<u8>,
    desktop: bool,
}

impl<W> AnsiOutput<W> {
    pub fn new(inner: W, desktop: bool) -> Self {
        Self {
            inner,
            pending: Vec::new(),
            desktop,
        }
    }
}

impl<W: Write> Write for AnsiOutput<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.pending.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.desktop {
            let mut output = Vec::with_capacity(self.pending.len());
            let mut i = 0;
            while i < self.pending.len() {
                if self.pending[i..].starts_with(b"\x1b[") {
                    let start = i + 2;
                    if let Some(end) = (start..self.pending.len())
                        .find(|&p| (0x40..=0x7e).contains(&self.pending[p]))
                        && self.pending[end] == b'm'
                    {
                        let parameters = &self.pending[start..end];
                        let fields: Vec<&[u8]> = parameters.split(|b| *b == b';').collect();
                        output.write_all(b"\x1b[")?;
                        let mut field = 0;
                        let mut first = true;
                        while field < fields.len() {
                            if !first {
                                output.write_all(b";")?;
                            }
                            first = false;
                            let color = if field + 2 < fields.len()
                                && matches!(fields[field], b"38" | b"48")
                                && fields[field + 1] == b"5"
                            {
                                std::str::from_utf8(fields[field + 2])
                                    .ok()
                                    .and_then(|s| s.parse::<u8>().ok())
                                    .filter(|n| *n < 16)
                            } else {
                                None
                            };
                            if let Some(color) = color {
                                let base = if fields[field] == b"38" { 30 } else { 40 };
                                let code = base + if color < 8 { color } else { color - 8 + 60 };
                                write!(output, "{code}")?;
                                field += 3;
                            } else {
                                output.write_all(fields[field])?;
                                field += 1;
                            }
                        }
                        output.write_all(b"m")?;
                        i = end + 1;
                        continue;
                    }
                }
                output.push(self.pending[i]);
                i += 1;
            }
            self.inner.write_all(&output)?;
        } else {
            self.inner.write_all(&self.pending)?;
        }
        self.pending.clear();
        self.inner.flush()
    }
}
