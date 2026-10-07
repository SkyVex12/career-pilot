//! The Copilot API (ARCHITECTURE.md, "REST API"), with the same token as the gateway.
//!
//! The UI renders what the API returns, so most calls hand back the JSON as it came; only the
//! uploads have types here, because the client does the work in between: a document goes
//! straight to S3 with the presigned URL from `POST /documents`, then `POST .../ingest`; the
//! user's own audio goes through `POST /sessions/{id}/me-audio` the same way
//! (see [`crate::recording`]).
//!
//! A `401` gets one token refresh and one retry, so a Cognito provider (build step 5) slots in
//! behind [`TokenProvider`] with no change here.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::auth::TokenProvider;

const JSON_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, thiserror::Error, PartialEq, Eq)]
pub enum ApiError {
    #[error("not signed in: {0}")]
    Auth(String),
    #[error("the API rejected the token; check it in Settings")]
    Unauthorized,
    #[error("{detail} (HTTP {status})")]
    Status { status: u16, detail: String },
    #[error("could not reach {0}")]
    Network(String),
    #[error("{0}")]
    Invalid(String),
}

impl ApiError {
    pub fn status(&self) -> Option<u16> {
        match self {
            Self::Status { status, .. } => Some(*status),
            Self::Unauthorized => Some(401),
            _ => None,
        }
    }

    pub fn detail(&self) -> &str {
        match self {
            Self::Status { detail, .. } => detail,
            _ => "",
        }
    }
}

/// Where and how to `PUT` an upload, as `POST /documents` and `POST .../me-audio` return it.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PresignedUpload {
    pub method: String,
    pub url: String,
    #[serde(default)]
    pub headers: HashMap<String, String>,
}

/// The document kinds the API ingests, and the content type a file name implies.
pub fn document_content_type(file_name: &str) -> Result<&'static str, ApiError> {
    let ext = file_name.rsplit_once('.').map(|(_, e)| e.to_ascii_lowercase()).unwrap_or_default();
    match ext.as_str() {
        "pdf" => Ok("application/pdf"),
        "md" | "markdown" => Ok("text/markdown"),
        "txt" => Ok("text/plain"),
        _ => Err(ApiError::Invalid(format!("{file_name}: upload a PDF, Markdown or text file"))),
    }
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

#[derive(Clone)]
pub struct ApiClient {
    base: String,
    http: reqwest::Client,
    tokens: Arc<dyn TokenProvider>,
}

impl ApiClient {
    /// `base_url` is the API's `/v1` root, for example `http://127.0.0.1:8080/v1`.
    pub fn new(base_url: &str, tokens: Arc<dyn TokenProvider>) -> Result<Self, ApiError> {
        let base = base_url.trim().trim_end_matches('/').to_string();
        if !(base.starts_with("http://") || base.starts_with("https://")) {
            return Err(ApiError::Invalid("the API URL needs http:// or https://".into()));
        }
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| ApiError::Invalid(e.to_string()))?;
        Ok(Self { base, http, tokens })
    }

    pub fn base_url(&self) -> &str {
        &self.base
    }

    async fn token(&self, refresh: bool) -> Result<String, ApiError> {
        let token = if refresh { self.tokens.refresh().await } else { self.tokens.current().await };
        token.map(|t| t.access_token).map_err(|e| ApiError::Auth(e.to_string()))
    }

    /// One JSON request. `204` and empty bodies come back as `Value::Null`.
    pub async fn request(&self, method: Method, path: &str, body: Option<&Value>) -> Result<Value, ApiError> {
        let url = format!("{}{path}", self.base);
        for attempt in 0..2 {
            let mut req = self
                .http
                .request(method.clone(), &url)
                .bearer_auth(self.token(attempt > 0).await?)
                .timeout(JSON_TIMEOUT);
            if let Some(body) = body {
                req = req.json(body);
            }
            let response = req.send().await.map_err(|e| ApiError::Network(network_detail(&self.base, &e)))?;
            let status = response.status();
            if status == StatusCode::UNAUTHORIZED && attempt == 0 {
                continue; // one refresh, then once more
            }
            let text = response.text().await.map_err(|e| ApiError::Network(e.to_string()))?;
            return parse_response(status, &text);
        }
        Err(ApiError::Unauthorized)
    }

    /// A call the UI names: `GET`, `POST`, `PATCH` or `DELETE` on a path under the base URL.
    pub async fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Result<Value, ApiError> {
        let method = match method.to_ascii_uppercase().as_str() {
            "GET" => Method::GET,
            "POST" => Method::POST,
            "PATCH" => Method::PATCH,
            "DELETE" => Method::DELETE,
            other => return Err(ApiError::Invalid(format!("method {other} is not allowed"))),
        };
        if !path.starts_with('/') || path.contains("://") || path.contains("..") {
            return Err(ApiError::Invalid(format!("bad API path {path:?}")));
        }
        self.request(method, path, body).await
    }

    pub async fn get(&self, path: &str) -> Result<Value, ApiError> {
        self.request(Method::GET, path, None).await
    }

    pub async fn post(&self, path: &str, body: &Value) -> Result<Value, ApiError> {
        self.request(Method::POST, path, Some(body)).await
    }

    pub async fn delete(&self, path: &str) -> Result<Value, ApiError> {
        self.request(Method::DELETE, path, None).await
    }

    /// `PUT` to a presigned S3 URL (no bearer token: the URL is the credential).
    pub async fn put_presigned(
        &self,
        upload: &PresignedUpload,
        body: reqwest::Body,
        len: u64,
    ) -> Result<(), ApiError> {
        if !upload.method.eq_ignore_ascii_case("PUT") {
            return Err(ApiError::Invalid(format!("unsupported upload method {}", upload.method)));
        }
        let mut req = self.http.put(&upload.url).header(reqwest::header::CONTENT_LENGTH, len).body(body);
        for (k, v) in &upload.headers {
            req = req.header(k, v);
        }
        let response = req.send().await.map_err(|e| ApiError::Network(network_detail(&upload.url, &e)))?;
        let status = response.status();
        if status.is_success() {
            return Ok(());
        }
        let text = response.text().await.unwrap_or_default();
        Err(ApiError::Status { status: status.as_u16(), detail: format!("upload refused: {}", text.trim()) })
    }

    /// A resume or job description: `POST /documents`, the upload, then ingest. Returns the
    /// document as `POST .../ingest` reports it (`status: ingesting`).
    pub async fn upload_document(
        &self,
        kind: &str,
        file_name: &str,
        bytes: Vec<u8>,
    ) -> Result<Value, ApiError> {
        let content_type = document_content_type(file_name)?;
        if bytes.is_empty() {
            return Err(ApiError::Invalid(format!("{file_name} is empty")));
        }
        let created = self
            .post(
                "/documents",
                &serde_json::json!({
                    "kind": kind,
                    "content_type": content_type,
                    "size_bytes": bytes.len(),
                    "sha256": sha256_hex(&bytes),
                }),
            )
            .await?;
        let id = created["document_id"]
            .as_str()
            .ok_or_else(|| ApiError::Invalid("no document_id".into()))?
            .to_string();
        let upload: PresignedUpload = serde_json::from_value(created["upload"].clone())
            .map_err(|e| ApiError::Invalid(format!("upload: {e}")))?;
        let len = bytes.len() as u64;
        self.put_presigned(&upload, reqwest::Body::from(bytes), len).await?;
        self.post(&format!("/documents/{id}/ingest"), &Value::Null).await
    }
}

fn network_detail(target: &str, e: &reqwest::Error) -> String {
    let host = target.split('/').take(3).collect::<Vec<_>>().join("/");
    if e.is_timeout() { format!("{host} (timed out)") } else { format!("{host} ({e})") }
}

fn parse_response(status: StatusCode, text: &str) -> Result<Value, ApiError> {
    let value =
        if text.trim().is_empty() { Value::Null } else { serde_json::from_str(text).unwrap_or(Value::Null) };
    if status.is_success() {
        return Ok(value);
    }
    if status == StatusCode::UNAUTHORIZED {
        return Err(ApiError::Unauthorized);
    }
    // FastAPI: {"detail": "..."}, or a list of validation errors for 422.
    let detail = match &value["detail"] {
        Value::String(s) => s.clone(),
        Value::Null => text.trim().chars().take(300).collect(),
        other => other.to_string(),
    };
    Err(ApiError::Status { status: status.as_u16(), detail })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_type_follows_the_extension() {
        assert_eq!(document_content_type("Resume.PDF").unwrap(), "application/pdf");
        assert_eq!(document_content_type("job.md").unwrap(), "text/markdown");
        assert_eq!(document_content_type("notes.txt").unwrap(), "text/plain");
        assert!(document_content_type("resume.docx").is_err());
        assert!(document_content_type("README").is_err());
    }

    #[test]
    fn errors_carry_fastapi_details() {
        let e = parse_response(StatusCode::CONFLICT, r#"{"detail": "live: not yet"}"#).unwrap_err();
        assert_eq!(e.status(), Some(409));
        assert_eq!(e.detail(), "live: not yet");
        assert_eq!(parse_response(StatusCode::NO_CONTENT, "").unwrap(), Value::Null);
        assert_eq!(parse_response(StatusCode::UNAUTHORIZED, "{}").unwrap_err(), ApiError::Unauthorized);
        let e = parse_response(StatusCode::BAD_GATEWAY, "upstream down").unwrap_err();
        assert_eq!(e.detail(), "upstream down");
    }

    #[test]
    fn sha256_is_lowercase_hex() {
        assert_eq!(sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }

    #[tokio::test]
    async fn calls_stay_under_the_base_url() {
        let api =
            ApiClient::new("http://127.0.0.1:9/v1", Arc::new(crate::auth::StaticTokens::one("t"))).unwrap();
        for (method, path) in [("PUT", "/me"), ("GET", "me"), ("GET", "http://evil/x"), ("GET", "/../admin")]
        {
            assert!(
                matches!(api.call(method, path, None).await, Err(ApiError::Invalid(_))),
                "{method} {path}"
            );
        }
    }

    #[test]
    fn base_url_needs_http() {
        let tokens = Arc::new(crate::auth::StaticTokens::one("t"));
        assert!(ApiClient::new("ws://x/v1", tokens.clone()).is_err());
        assert_eq!(ApiClient::new("http://x/v1/ ", tokens).unwrap().base_url(), "http://x/v1");
    }
}
