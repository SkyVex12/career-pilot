//! The on-demand server (Stage 0 on GCP, infra/gcp/README.md "Running on demand"): the VM
//! stops itself when nobody uses it. The wake function (infra/gcp/wake) starts the VM and
//! answers with its address, and [`start`] waits until the configured API answers. A stage can
//! use a fixed HTTPS domain or legacy URLs carrying the VM address placeholder `{host}`.

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

/// Whether a URL belongs to the on-demand stage. Match origins so session profile queries
/// and the derived seat endpoint also wake it, while unrelated local/custom servers do not.
/// `stage_urls` is present only when the build configures a wake function for those endpoints.
pub fn uses_wake(url: &str, stage_urls: Option<(&str, &str)>) -> bool {
    if needs_host(url) {
        return true;
    }
    let Some((api, gateway)) = stage_urls else { return false };
    let Ok(url) = reqwest::Url::parse(url) else { return false };
    if !matches!(url.scheme(), "http" | "https" | "ws" | "wss") || !url.has_host() {
        return false;
    }
    [api, gateway]
        .into_iter()
        .any(|endpoint| reqwest::Url::parse(endpoint).is_ok_and(|endpoint| endpoint.origin() == url.origin()))
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
    let response = http()
        .post(wake_url)
        .json(&serde_json::json!({}))
        .send()
        .await
        .map_err(|e| format!("could not reach the wake function: {e}"))?;
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
    fn fixed_stage_urls_use_wake_without_capturing_other_servers() {
        let stage = Some(("https://coruby.xruby.tech/v1", "wss://coruby.xruby.tech/v1/session"));
        for url in [
            "https://coruby.xruby.tech/v1",
            "https://coruby.xruby.tech:443/v1",
            "wss://coruby.xruby.tech/v1/session?profile=ab-12",
            "wss://coruby.xruby.tech/v1/seat",
        ] {
            assert!(uses_wake(url, stage), "{url}");
            assert!(!uses_wake(url, None), "an always-on build must not wake: {url}");
        }
        for url in [
            "http://127.0.0.1:8080/v1",
            "ws://localhost:8081/v1/session",
            "https://custom.example/v1",
            "https://coruby.xruby.tech.example/v1",
            "https://coruby.xruby.tech:8443/v1",
            "http://coruby.xruby.tech/v1",
            "not a URL",
        ] {
            assert!(!uses_wake(url, stage), "{url}");
        }
        assert!(uses_wake("http://{host}/v1", None));
    }

    #[tokio::test]
    async fn fixed_api_is_checked_after_waking_without_replacing_its_host() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        async fn serve(body: &str) -> (String, tokio::task::JoinHandle<String>) {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let task = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut bytes = [0; 4096];
                let len = stream.read(&mut bytes).await.unwrap();
                let request = String::from_utf8_lossy(&bytes[..len]).into_owned();
                stream.write_all(response.as_bytes()).await.unwrap();
                request.lines().next().unwrap().to_string()
            });
            (url, task)
        }

        let (wake_url, wake_request) = serve(r#"{"status":"RUNNING","host":"127.0.0.2"}"#).await;
        let (api_origin, api_request) = serve(r#"{"provider":"google"}"#).await;
        let api_url = format!("{api_origin}/v1");
        let host = tokio::time::timeout(Duration::from_secs(5), start(&wake_url, &api_url, |_| {}))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(host, "127.0.0.2");
        assert_eq!(fill(&api_url, &host), api_url);
        assert_eq!(wake_request.await.unwrap(), "POST / HTTP/1.1");
        assert_eq!(api_request.await.unwrap(), "GET /v1/auth/config HTTP/1.1");
    }

    #[test]
    fn reads_the_reply() {
        let r: Reply = serde_json::from_str(r#"{"status":"RUNNING","host":"34.1.2.3"}"#).unwrap();
        assert_eq!(r, Reply { status: "RUNNING".into(), host: Some("34.1.2.3".into()) });
        let r: Reply = serde_json::from_str(r#"{"status":"TERMINATED","host":null}"#).unwrap();
        assert_eq!(r.host, None);
    }
}

/// Against a real stage: `COPILOT_WAKE_URL=... COPILOT_API_URL='http://{host}/v1' cargo test -p
/// copilot-client wake::live -- --ignored`. It starts the VM if it is stopped.
#[cfg(test)]
mod live {
    #[tokio::test]
    #[ignore = "needs a deployed stage"]
    async fn starts_the_stage_and_finds_its_api() {
        let wake_url = std::env::var("COPILOT_WAKE_URL").expect("COPILOT_WAKE_URL");
        let api_url = std::env::var("COPILOT_API_URL").unwrap_or_else(|_| "http://{host}/v1".into());
        let host = super::start(&wake_url, &api_url, |p| eprintln!("{p:?}")).await.unwrap();
        assert!(super::api_ready(&super::fill(&api_url, &host)).await);
    }
}
