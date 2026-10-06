//! Runs `python -m copilot_gateway.offline` for the integration tests.
#![allow(dead_code)]

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command as Proc, Stdio};

pub const TOKEN: &str = "test-token-0123456789";

pub fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap()
}

/// The workspace's Python directly, not `uv run`: killing `uv` would leave Python running.
pub fn python() -> PathBuf {
    let root = repo_root();
    let win = root.join(".venv/Scripts/python.exe");
    let unix = root.join(".venv/bin/python");
    if win.exists() {
        win
    } else if unix.exists() {
        unix
    } else {
        panic!("no .venv in {}; run `uv sync` first", root.display())
    }
}

pub fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

pub struct Gateway {
    child: Child,
    pub port: u16,
}

impl Gateway {
    pub fn start(port: u16, tokens: &[&str], valkey: Option<&str>) -> Self {
        let mut cmd = Proc::new(python());
        cmd.current_dir(repo_root())
            .args(["-m", "copilot_gateway.offline", "--fast-timers", "--port", &port.to_string()])
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        for t in tokens {
            cmd.args(["--token", t]);
        }
        if let Some(url) = valkey {
            cmd.args(["--valkey", url]);
        }
        let mut child = cmd.spawn().expect("start the offline gateway");
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap()).read_line(&mut line).unwrap();
        let port = line.trim().strip_prefix("listening ").unwrap_or_else(|| panic!("gateway said {line:?}"));
        Self { child, port: port.parse().unwrap() }
    }

    pub fn url(&self) -> String {
        format!("ws://127.0.0.1:{}/v1/session", self.port)
    }

    /// As a crash would: no close frames, no cleanup.
    pub fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for Gateway {
    fn drop(&mut self) {
        self.kill();
    }
}
