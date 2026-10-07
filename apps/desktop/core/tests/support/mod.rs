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

/// `scripts/desktop_test_stack.py`: the real API, a worker on recorded fixtures and the
/// gateway (scripted STT and Claude), on the compose stack.
pub struct Stack {
    child: Child,
    pub api: String,
    pub gateway: String,
    pub token: String,
}

impl Stack {
    /// Panics with the reason when the compose stack is down.
    pub fn start() -> Self {
        let mut child = Proc::new(python())
            .current_dir(repo_root())
            .arg("scripts/desktop_test_stack.py")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("start scripts/desktop_test_stack.py");
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap()).read_line(&mut line).unwrap();
        if line.trim().is_empty() || line.starts_with("unavailable") {
            let _ = child.wait();
            panic!(
                "the API integration tests need the local stack (`docker compose up -d --wait`): {}",
                line.trim()
            );
        }
        let info: serde_json::Value = serde_json::from_str(&line).unwrap_or_else(|e| panic!("{e}: {line:?}"));
        Self {
            child,
            api: info["api"].as_str().unwrap().into(),
            gateway: info["gateway"].as_str().unwrap().into(),
            token: info["token"].as_str().unwrap().into(),
        }
    }
}

impl Drop for Stack {
    /// Closing stdin lets the harness drop its database and queue; kill it if it hangs.
    fn drop(&mut self) {
        drop(self.child.stdin.take());
        for _ in 0..200 {
            if let Ok(Some(_)) = self.child.try_wait() {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
