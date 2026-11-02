//! The on-demand server (Stage 0 on GCP, infra/gcp/README.md "Running on demand"): the VM
//! stops itself when nobody uses it, and its address changes at every start. A release build's
//! URLs carry `{host}`; the wake function (infra/gcp/wake) starts the VM and answers with its
//! address, and [`start`] waits until the API behind it answers.

use std::time::Duration;

use serde::Deserialize;

/// The placeholder in the stage's URLs for the VM's current address.
pub const HOST: &str = "{host}";

/// How long a start may take: the VM boots and its containers come up in one to two minutes.
pub const START_TIMEOUT: Duration = Duration::from_secs(300);
const POLL: Duration = Duration::from_secs(4);
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

/// Whether the URL needs the VM's address filled in.
pub fn needs_host(url: &str) -> bool {
    url.contains(HOST)
}

/// The URL with the VM's address in place of `{host}`.
pub fn fill(url: &str, host: &str) -> String {
    url.replace(HOST, host)
}

/// The wake function's answer: the VM's state (Compute Engine's: RUNNING, STAGING,
/// TERMINATED, ...) and, while it runs, its address.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Reply {
    pub status: String,
    pub host: Option<String>,
}

/// What a start is doing, for the app's "Starting the server" note.
#[derive(Debug, Clone, PartialEq)]
pub enum Progress {
    /// The VM is booting (its Compute Engine state).
    Vm(String),
    /// The VM runs; its services are coming up.
    Services,
}

fn http() -> reqwest::Client {
    reqwest::Client::builder().timeout(HTTP_TIMEOUT).build().expect("an HTTP client")
}

/// POST: start the VM if it is stopped; its state and address.
pub async fn wake(wake_url: &str) -> Result<Reply, String> {
    let response =
        http().post(wake_url).send().await.map_err(|e| format!("could not reach the wake function: {e}"))?;
    let status = response.status();
    if !status.is_success() {
        return Err(format!("the wake function answered HTTP {}", status.as_u16()));
    }
    response.json().await.map_err(|e| format!("the wake function's answer: {e}"))
}

/// Whether the API at `api_url` (filled in) answers: its public sign-in settings.
pub async fn api_ready(api_url: &str) -> bool {
    let url = format!("{}/auth/config", api_url.trim_end_matches('/'));
    matches!(http().get(&url).send().await, Ok(r) if r.status().is_success())
}

/// Start the VM (if it is stopped) and wait until its API answers; the VM's address.
pub async fn start(wake_url: &str, api_url: &str, progress: impl Fn(Progress)) -> Result<String, String> {
    let deadline = tokio::time::Instant::now() + START_TIMEOUT;
    let mut last_error = String::new();
    loop {
        match wake(wake_url).await {
            Ok(Reply { status, host: Some(host) }) if status == "RUNNING" => {
                if api_ready(&fill(api_url, &host)).await {
                    return Ok(host);
                }
                progress(Progress::Services);
            }
            Ok(reply) => progress(Progress::Vm(reply.status)),
            Err(e) => last_error = e,
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(if last_error.is_empty() {
                "The server did not start within five minutes. Try again in a minute.".into()
            } else {
                format!("The server did not start: {last_error}")
            });
        }
        tokio::time::sleep(POLL).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fills_the_host() {
        assert!(needs_host("http://{host}/v1"));
        assert!(!needs_host("http://127.0.0.1:8080/v1"));
        assert_eq!(
            fill("ws://{host}/v1/session?profile=ab", "34.1.2.3"),
            "ws://34.1.2.3/v1/session?profile=ab"
        );
    }

    #[test]
    fn reads_the_reply() {
        let r: Reply = serde_json::from_str(r#"{"status":"RUNNING","host":"34.1.2.3"}"#).unwrap();
        assert_eq!(r, Reply { status: "RUNNING".into(), host: Some("34.1.2.3".into()) });
        let r: Reply = serde_json::from_str(r#"{"status":"TERMINATED","host":null}"#).unwrap();
        assert_eq!(r.host, None);
    }
}
