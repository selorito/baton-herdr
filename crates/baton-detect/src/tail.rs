//! Following a growing log file: `tail -F` for one file, without the process.

use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

/// Hands out each complete line of a file once, as the file grows.
///
/// A line is complete when its newline is written; a partial last line waits for the
/// rest. If the file becomes shorter than what was read, it was replaced or truncated:
/// reading starts again from the beginning and [`Tail::read_lines`] says so.
#[derive(Debug)]
pub struct Tail {
    path: PathBuf,
    offset: u64,
    partial: Vec<u8>,
}

/// New lines, and whether the file started over since the last read.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Lines {
    pub lines: Vec<String>,
    pub restarted: bool,
}

impl Tail {
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            offset: 0,
            partial: Vec::new(),
        }
    }

    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The complete lines written since the last call.
    ///
    /// # Errors
    /// The file cannot be opened or read.
    pub fn read_lines(&mut self) -> io::Result<Lines> {
        let mut file = File::open(&self.path)?;
        let len = file.metadata()?.len();
        let mut result = Lines::default();
        if len < self.offset {
            self.offset = 0;
            self.partial.clear();
            result.restarted = true;
        }
        if len == self.offset {
            return Ok(result);
        }
        file.seek(SeekFrom::Start(self.offset))?;
        let mut buffer = Vec::new();
        file.take(len - self.offset).read_to_end(&mut buffer)?;
        self.offset += buffer.len() as u64;
        self.partial.extend_from_slice(&buffer);
        if let Some(end) = self.partial.iter().rposition(|&b| b == b'\n') {
            let complete: Vec<u8> = self.partial.drain(..=end).collect();
            result.lines = String::from_utf8_lossy(&complete)
                .lines()
                .filter(|line| !line.trim().is_empty())
                .map(str::to_owned)
                .collect();
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;

    fn temp_file(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("baton-detect-tail-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        File::create(&path).unwrap();
        path
    }

    fn append(path: &Path, text: &str) {
        let mut file = std::fs::OpenOptions::new().append(true).open(path).unwrap();
        file.write_all(text.as_bytes()).unwrap();
    }

    #[test]
    fn complete_lines_once_and_partial_lines_when_they_end() {
        let path = temp_file("grow.jsonl");
        let mut tail = Tail::new(path.clone());
        append(&path, "one\ntw");
        assert_eq!(tail.read_lines().unwrap().lines, ["one"]);
        assert!(tail.read_lines().unwrap().lines.is_empty());
        append(&path, "o\n\nthree\n");
        assert_eq!(tail.read_lines().unwrap().lines, ["two", "three"]);
    }

    #[test]
    fn a_truncated_file_is_read_again_from_the_start() {
        let path = temp_file("truncate.jsonl");
        let mut tail = Tail::new(path.clone());
        append(&path, "a long first line\n");
        tail.read_lines().unwrap();
        std::fs::write(&path, "new\n").unwrap();
        let lines = tail.read_lines().unwrap();
        assert!(lines.restarted);
        assert_eq!(lines.lines, ["new"]);
    }
}
