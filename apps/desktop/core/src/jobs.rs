//! Jobs for the start screen. Until the Copilot API exists (build step 4, `GET /jobs`), they
//! come from the gateway's dev route `GET /v1/dev/jobs`, which reads the pack index that
//! scripts/dev_profile.py writes. [`JobsSource`] is what the API client replaces.

use serde::{Deserialize, Serialize};

use crate::auth::TokenProvider;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Job {
    pub job_id: String,
    pub pack_id: String,
    pub pack_available: bool,
    pub company: Option<String>,
    pub role_title: Option<String>,
    pub round: Option<String>,
    pub built_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct JobList {
    pub user_name: String,
    pub jobs: Vec<Job>,
    /// Modes with a pack of their own (`mode:{mode}` in the index).
    pub mode_packs: Vec<String>,
}

/// `ws[s]://host/v1/session` to `http[s]://host/v1/dev/jobs`.
pub fn dev_jobs_url(session_url: &str) -> Result<String, String> {
    let (scheme, rest) = session_url.split_once("://").ok_or("the gateway URL needs ws:// or wss://")?;
    let http = match scheme {
        "ws" => "http",
        "wss" => "https",
        other => return Err(format!("unsupported scheme {other}")),
    };
    let host = rest.split('/').next().unwrap_or(rest);
    Ok(format!("{http}://{host}/v1/dev/jobs"))
}

pub struct JobsSource {
    pub session_url: String,
}

impl JobsSource {
    pub async fn list(&self, tokens: &dyn TokenProvider) -> Result<JobList, String> {
        let url = dev_jobs_url(&self.session_url)?;
        let token = tokens.current().await.map_err(|e| e.to_string())?;
        let response = reqwest::Client::new()
            .get(&url)
            .bearer_auth(token.access_token)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .map_err(|e| format!("could not reach the gateway: {e}"))?;
        match response.status().as_u16() {
            200 => response.json().await.map_err(|e| e.to_string()),
            401 => Err("the gateway rejected the dev token".into()),
            404 => Err("this gateway has no dev routes (COPILOT_ENV must be dev)".into()),
            s => Err(format!("gateway returned HTTP {s}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::dev_jobs_url;

    #[test]
    fn derives_the_dev_jobs_url() {
        assert_eq!(
            dev_jobs_url("ws://127.0.0.1:8081/v1/session").unwrap(),
            "http://127.0.0.1:8081/v1/dev/jobs"
        );
        assert_eq!(
            dev_jobs_url("wss://rt.example.com/v1/session").unwrap(),
            "https://rt.example.com/v1/dev/jobs"
        );
        assert!(dev_jobs_url("http://x/v1/session").is_err());
    }
}
