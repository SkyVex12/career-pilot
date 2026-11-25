//! Email sign-in (ARCHITECTURE.md, "Email sign-in"): the user types their address, the API
//! mails a six-digit code, and the code is traded for an access token and a refresh token.
//! No password exists, and nothing that could be used twice is sent: a code works once, and
//! every refresh replaces the refresh token.
//!
//! The refresh token lives where the browser sign-in's does: the OS credential store (Windows
//! Credential Manager). The access token lives in memory only.
//!
//! [`EmailTokenProvider`] is a [`TokenProvider`] like [`crate::oauth::OAuthTokenProvider`], so
//! the gateway client and the API client use it unchanged: `auth.refresh` before the token
//! expires and on `auth.expiring`, and one refresh after a `401`.
//!
//! Unlike Google's, these tokens come from the stage's own API, whose address changes with
//! every start of its VM and which may be asleep ([`crate::wake`]). The provider asks
//! [`ApiBase`] for the API's address each time, and the app answers after waking the server.
//!
//! The server revokes the whole sign-in when a refresh token is shown twice, so refreshes are
//! serialized here: two callers that ask at once share one refresh.

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use serde::{Deserialize, Serialize};

use crate::auth::{AuthError, BoxFuture, SecretStore, Token, TokenProvider};

/// An access token this close to its expiry is refreshed before use.
const EXPIRY_MARGIN: Duration = Duration::from_secs(120);
/// A token this new is handed to a second caller instead of being refreshed again.
const JUST_REFRESHED: Duration = Duration::from_secs(10);
const HTTP_TIMEOUT: Duration = Duration::from_secs(20);

/// Where the stage's API is right now: its `/v1` root, with the server started if it slept.
pub trait ApiBase: Send + Sync {
    fn api_url(&self) -> BoxFuture<'_, Result<String, String>>;
}

/// A fixed URL: the local stack, and tests.
pub struct FixedApi(pub String);

impl ApiBase for FixedApi {
    fn api_url(&self) -> BoxFuture<'_, Result<String, String>> {
        Box::pin(async move { Ok(self.0.clone()) })
    }
}

/// `POST /auth/email/start`'s answer: what the code screen needs.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CodeSent {
    #[serde(default = "six")]
    pub code_length: u32,
    /// How long the code works.
    #[serde(default)]
    pub expires_in_s: u64,
    /// How long until the app offers to send another.
    #[serde(default)]
    pub resend_after_s: u64,
}

fn six() -> u32 {
    6
}

/// Why a step of the sign-in failed, for the screen to put its own words to. `code` is the
/// API's (`invalid_email`, `invalid_code`, `code_expired`, `code_locked`, `rate_limited`,
/// `email_taken`, `unavailable`), or `not_offered` (the stage has no email sign-in) or
/// `network` (the API did not answer).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct EmailFailure {
    pub code: String,
    pub message: String,
    /// `rate_limited`: when the next try can work.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after_s: Option<u64>,
}

impl EmailFailure {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self { code: code.into(), message: message.into(), retry_after_s: None }
    }

    pub fn network(what: impl std::fmt::Display) -> Self {
        Self::new("network", format!("Could not reach the server: {what}"))
    }
}

#[derive(Debug, Deserialize)]
struct Problem {
    detail: serde_json::Value,
}

/// The API's error body: `{"detail": {"code", "message", "retry_after_s"?}}`, or a plain
/// `{"detail": "..."}` for anything else.
fn failure(status: u16, body: &str) -> EmailFailure {
    let detail = serde_json::from_str::<Problem>(body).map(|p| p.detail).unwrap_or_default();
    if let Ok(f) = serde_json::from_value::<EmailFailure>(detail.clone()) {
        return f;
    }
    if status == 404 {
        return EmailFailure::new("not_offered", "This server does not offer email sign-in.");
    }
    let text = detail.as_str().map(str::to_string).unwrap_or_else(|| format!("HTTP {status}"));
    EmailFailure::new("unavailable", text)
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: String,
    expires_in: u64,
    #[serde(default)]
    email: String,
}

struct Cached {
    token: Token,
    at: Instant,
}

pub struct EmailTokenProvider {
    /// Names the stage in the credential store: its API URL as configured (`{host}` and all).
    stage: String,
    base: Arc<dyn ApiBase>,
    store: Arc<dyn SecretStore>,
    http: reqwest::Client,
    /// Held across a refresh, so there is one at a time.
    cached: tokio::sync::Mutex<Option<Cached>>,
}

impl EmailTokenProvider {
    pub fn new(stage: impl Into<String>, base: Arc<dyn ApiBase>, store: Arc<dyn SecretStore>) -> Self {
        Self {
            stage: stage.into(),
            base,
            store,
            http: reqwest::Client::builder().timeout(HTTP_TIMEOUT).build().expect("an HTTP client"),
            cached: tokio::sync::Mutex::new(None),
        }
    }

    pub fn stage(&self) -> &str {
        &self.stage
    }

    fn refresh_key(&self) -> String {
        format!("email-refresh:{}", self.stage)
    }

    fn user_key(&self) -> String {
        format!("email-user:{}", self.stage)
    }

    /// Who is signed in with an emailed code, if anyone: the address.
    pub fn signed_in_as(&self) -> Option<String> {
        match self.store.get(&self.refresh_key()) {
            Ok(Some(_)) => Some(self.store.get(&self.user_key()).ok().flatten().unwrap_or_default()),
            _ => None,
        }
    }

    async fn post(&self, path: &str, body: serde_json::Value) -> Result<String, EmailFailure> {
        let base = self.base.api_url().await.map_err(|e| EmailFailure::new("network", e))?;
        let url = format!("{}/auth/email/{path}", base.trim_end_matches('/'));
        let response = self.http.post(&url).json(&body).send().await.map_err(EmailFailure::network)?;
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        if status.is_success() { Ok(text) } else { Err(failure(status.as_u16(), &text)) }
    }

    /// Ask for a code. The answer is the same whether or not the address has an account.
    pub async fn start(&self, email: &str) -> Result<CodeSent, EmailFailure> {
        let text = self.post("start", serde_json::json!({ "email": email })).await?;
        serde_json::from_str(&text).map_err(|e| EmailFailure::new("unavailable", e.to_string()))
    }

    /// Trade the code for tokens and keep the refresh token. Returns the address signed in.
    pub async fn verify(&self, email: &str, code: &str) -> Result<String, EmailFailure> {
        let text = self.post("verify", serde_json::json!({ "email": email, "code": code })).await?;
        let tokens: TokenResponse =
            serde_json::from_str(&text).map_err(|e| EmailFailure::new("unavailable", e.to_string()))?;
        let signed_in =
            if tokens.email.is_empty() { email.trim().to_lowercase() } else { tokens.email.clone() };
        let stored = self
            .store
            .set(&self.refresh_key(), &tokens.refresh_token)
            .and_then(|()| self.store.set(&self.user_key(), &signed_in));
        stored.map_err(|e| EmailFailure::new("unavailable", e.to_string()))?;
        *self.cached.lock().await = Some(Cached { token: token_of(&tokens), at: Instant::now() });
        Ok(signed_in)
    }

    /// Forget the tokens here and revoke the sign-in at the API (best effort: a server that is
    /// asleep is not woken for it, and the refresh token is gone from this computer either way).
    pub async fn sign_out(&self) -> Result<(), AuthError> {
        let (refresh, cleared) = {
            // Wait for any token rotation, then forget access, refresh and user together.
            let mut cached = self.cached.lock().await;
            let refresh = self.store.get(&self.refresh_key()).ok().flatten();
            *cached = None;
            let refresh_deleted = self.store.delete(&self.refresh_key());
            let user_deleted = self.store.delete(&self.user_key());
            (refresh, refresh_deleted.and(user_deleted))
        };
        if let Some(refresh) = refresh {
            let _ = self.post("signout", serde_json::json!({ "refresh_token": refresh })).await;
        }
        cleared
    }

    /// `force`: the caller's token was refused, so a cached one is no good unless it is newer
    /// than the caller's (another caller refreshed a moment ago).
    async fn token(&self, force: bool) -> Result<Token, AuthError> {
        let mut cached = self.cached.lock().await;
        if let Some(c) = cached.as_ref() {
            let fresh = c.token.expires_at.is_some_and(|at| at > SystemTime::now() + EXPIRY_MARGIN);
            if fresh && (!force || c.at.elapsed() < JUST_REFRESHED) {
                return Ok(c.token.clone());
            }
        }
        let Some(refresh) = self.store.get(&self.refresh_key())? else {
            return Err(AuthError::NotSignedIn("sign in under Settings".into()));
        };
        match self.post("refresh", serde_json::json!({ "refresh_token": refresh })).await {
            Ok(text) => {
                let tokens: TokenResponse = serde_json::from_str(&text)
                    .map_err(|e| AuthError::NotSignedIn(format!("token response: {e}")))?;
                // The old refresh token is spent the moment the server answers: keep the new one
                // before anything else.
                self.store.set(&self.refresh_key(), &tokens.refresh_token)?;
                let token = token_of(&tokens);
                *cached = Some(Cached { token: token.clone(), at: Instant::now() });
                Ok(token)
            }
            Err(f) if f.code == "invalid_refresh_token" => {
                // Expired, signed out elsewhere, or revoked.
                let _ = self.store.delete(&self.refresh_key());
                *cached = None;
                Err(AuthError::NotSignedIn("the sign-in expired; sign in again under Settings".into()))
            }
            Err(f) => Err(AuthError::NotSignedIn(f.message)),
        }
    }
}

fn token_of(tokens: &TokenResponse) -> Token {
    Token {
        access_token: tokens.access_token.clone(),
        expires_at: Some(SystemTime::now() + Duration::from_secs(tokens.expires_in)),
    }
}

impl TokenProvider for EmailTokenProvider {
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(self.token(false))
    }

    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(self.token(true))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::MemorySecretStore;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    /// A stand-in for the API: answers each POST with the next canned reply and records the
    /// request line and body.
    async fn fake_api(replies: Vec<(u16, String)>) -> (String, Arc<std::sync::Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let seen2 = seen.clone();
        tokio::spawn(async move {
            for (status, body) in replies {
                let (mut s, _) = listener.accept().await.unwrap();
                let mut buf = vec![0u8; 16384];
                let mut got = 0;
                loop {
                    let n = s.read(&mut buf[got..]).await.unwrap();
                    got += n;
                    let text = String::from_utf8_lossy(&buf[..got]).to_string();
                    if let Some((head, rest)) = text.split_once("\r\n\r\n") {
                        let len = head
                            .lines()
                            .find_map(|l| {
                                l.to_ascii_lowercase()
                                    .strip_prefix("content-length:")
                                    .map(|v| v.trim().parse::<usize>().unwrap())
                            })
                            .unwrap_or(0);
                        if rest.len() >= len || n == 0 {
                            let line = head.lines().next().unwrap_or_default();
                            seen2.lock().unwrap().push(format!("{line} {rest}"));
                            break;
                        }
                    }
                }
                let reply = format!(
                    "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                s.write_all(reply.as_bytes()).await.unwrap();
            }
        });
        (format!("http://{addr}/v1"), seen)
    }

    fn tokens(access: &str, refresh: &str, expires_in: u64) -> String {
        format!(
            "{{\"access_token\":\"{access}\",\"refresh_token\":\"{refresh}\",\"token_type\":\"Bearer\",\
             \"expires_in\":{expires_in},\"email\":\"sam@example.com\"}}"
        )
    }

    fn provider(url: &str) -> (EmailTokenProvider, Arc<MemorySecretStore>) {
        let store = Arc::new(MemorySecretStore::default());
        let p = EmailTokenProvider::new("http://{host}/v1", Arc::new(FixedApi(url.into())), store.clone());
        (p, store)
    }

    #[tokio::test]
    async fn the_code_signs_in_and_the_refresh_token_is_kept() {
        let sent = "{\"sent\":true,\"code_length\":6,\"expires_in_s\":600,\"resend_after_s\":60}";
        let (url, seen) = fake_api(vec![(200, sent.into()), (200, tokens("at-1", "rt-1", 3600))]).await;
        let (p, store) = provider(&url);
        assert!(p.signed_in_as().is_none());
        assert!(matches!(p.current().await, Err(AuthError::NotSignedIn(_))));

        let started = p.start("Sam@Example.com").await.unwrap();
        assert_eq!(started, CodeSent { code_length: 6, expires_in_s: 600, resend_after_s: 60 });
        assert_eq!(p.verify("Sam@Example.com", "123456").await.unwrap(), "sam@example.com");
        assert_eq!(p.signed_in_as().as_deref(), Some("sam@example.com"));
        assert_eq!(store.get("email-refresh:http://{host}/v1").unwrap().as_deref(), Some("rt-1"));
        // The access token is in memory only.
        assert!(store.get("email-user:http://{host}/v1").unwrap().as_deref() == Some("sam@example.com"));
        assert_eq!(p.current().await.unwrap().access_token, "at-1");
        let seen = seen.lock().unwrap();
        assert!(seen[0].starts_with("POST /v1/auth/email/start "));
        assert!(seen[0].contains("\"email\":\"Sam@Example.com\""));
        assert!(seen[1].starts_with("POST /v1/auth/email/verify "));
        assert!(seen[1].contains("\"code\":\"123456\""));
    }

    #[tokio::test]
    async fn failures_carry_the_api_code() {
        let wrong = "{\"detail\":{\"code\":\"invalid_code\",\"message\":\"That code is not right.\"}}";
        let expired = "{\"detail\":{\"code\":\"code_expired\",\"message\":\"Expired.\"}}";
        let limited = "{\"detail\":{\"code\":\"rate_limited\",\"message\":\"Wait.\",\"retry_after_s\":840}}";
        let (url, _) = fake_api(vec![
            (400, wrong.into()),
            (400, expired.into()),
            (429, limited.into()),
            (404, "{\"detail\":\"Not Found\"}".into()),
            (500, "oops".into()),
        ])
        .await;
        let (p, store) = provider(&url);
        assert_eq!(p.verify("a@b.co", "000000").await.unwrap_err().code, "invalid_code");
        assert_eq!(p.verify("a@b.co", "000000").await.unwrap_err().code, "code_expired");
        let f = p.start("a@b.co").await.unwrap_err();
        assert_eq!((f.code.as_str(), f.retry_after_s), ("rate_limited", Some(840)));
        assert_eq!(p.start("a@b.co").await.unwrap_err().code, "not_offered");
        assert_eq!(p.start("a@b.co").await.unwrap_err().code, "unavailable");
        // Nothing was stored by a failed sign-in.
        assert!(p.signed_in_as().is_none());
        assert!(store.get("email-refresh:http://{host}/v1").unwrap().is_none());
        // A server that cannot be reached.
        let (gone, _) = provider("http://127.0.0.1:1/v1");
        assert_eq!(gone.start("a@b.co").await.unwrap_err().code, "network");
    }

    #[tokio::test]
    async fn refresh_rotates_the_stored_token_and_an_ended_sign_in_signs_out() {
        let (url, seen) = fake_api(vec![
            (200, tokens("at-1", "rt-1", 3600)),
            (200, tokens("at-2", "rt-2", 3600)),
            (401, "{\"detail\":{\"code\":\"invalid_refresh_token\",\"message\":\"Ended.\"}}".into()),
        ])
        .await;
        let (p, store) = provider(&url);
        p.verify("sam@example.com", "123456").await.unwrap();
        // The gateway client asks for a fresh token after a 4401, moments after sign-in: the
        // token just obtained is handed back, with no second refresh.
        assert_eq!(p.refresh().await.unwrap().access_token, "at-1");
        assert_eq!(seen.lock().unwrap().len(), 1);

        // Later (the cached token no longer counts as just refreshed): a real rotation.
        p.cached.lock().await.as_mut().unwrap().at = Instant::now() - Duration::from_secs(60);
        assert_eq!(p.refresh().await.unwrap().access_token, "at-2");
        assert!(seen.lock().unwrap()[1].contains("\"refresh_token\":\"rt-1\""));
        assert_eq!(store.get("email-refresh:http://{host}/v1").unwrap().as_deref(), Some("rt-2"));
        assert_eq!(p.current().await.unwrap().access_token, "at-2");

        // The server ended the sign-in: the app is signed out, and says so.
        p.cached.lock().await.as_mut().unwrap().at = Instant::now() - Duration::from_secs(60);
        assert!(matches!(p.refresh().await, Err(AuthError::NotSignedIn(m)) if m.contains("sign in again")));
        assert!(p.signed_in_as().is_none());
    }

    #[tokio::test]
    async fn two_callers_share_one_refresh() {
        // One reply only: a second request would hang the test's server and fail the call.
        let (url, seen) = fake_api(vec![(200, tokens("at-2", "rt-2", 3600))]).await;
        let (p, store) = provider(&url);
        store.set("email-refresh:http://{host}/v1", "rt-1").unwrap();
        let p = Arc::new(p);
        let (a, b) = tokio::join!(p.current(), p.refresh());
        assert_eq!(a.unwrap().access_token, "at-2");
        assert_eq!(b.unwrap().access_token, "at-2");
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn an_expiring_token_is_refreshed_before_use_and_a_down_server_keeps_the_sign_in() {
        let (url, _) =
            fake_api(vec![(200, tokens("at-1", "rt-1", 60)), (200, tokens("at-2", "rt-2", 3600))]).await;
        let (p, store) = provider(&url);
        p.verify("sam@example.com", "123456").await.unwrap();
        // 60 s left is inside the margin.
        assert_eq!(p.current().await.unwrap().access_token, "at-2");

        // The server does not answer: an error, but the user stays signed in.
        let down = EmailTokenProvider::new(
            "stage",
            Arc::new(FixedApi("http://127.0.0.1:1/v1".into())),
            store.clone(),
        );
        store.set("email-refresh:stage", "rt-9").unwrap();
        store.set("email-user:stage", "sam@example.com").unwrap();
        assert!(
            matches!(down.current().await, Err(AuthError::NotSignedIn(m)) if m.contains("Could not reach"))
        );
        assert_eq!(down.signed_in_as().as_deref(), Some("sam@example.com"));
    }

    #[tokio::test]
    async fn sign_out_revokes_the_latest_refresh_and_stays_signed_out_when_revocation_fails() {
        let (url, seen) = fake_api(vec![(200, tokens("at-1", "rt-1", 3600)), (503, String::new())]).await;
        let (p, store) = provider(&url);
        p.verify("sam@example.com", "123456").await.unwrap();

        // A refresh owns this lock while the server rotates the saved refresh token.
        let cached = p.cached.lock().await;
        let mut signing_out = Box::pin(p.sign_out());
        tokio::select! {
            biased;
            result = &mut signing_out => panic!("sign-out did not wait for token rotation: {result:?}"),
            _ = std::future::ready(()) => {}
        }
        store.set(&p.refresh_key(), "rt-2").unwrap();
        drop(cached);
        signing_out.await.unwrap();

        assert!(seen.lock().unwrap()[1].contains("\"refresh_token\":\"rt-2\""));
        assert!(store.get(&p.refresh_key()).unwrap().is_none());
        assert!(store.get(&p.user_key()).unwrap().is_none());
        assert!(p.signed_in_as().is_none());
        assert!(matches!(p.current().await, Err(AuthError::NotSignedIn(_))));
    }

    #[tokio::test]
    async fn sign_out_forgets_the_tokens_and_tells_the_server() {
        let (url, seen) = fake_api(vec![(200, tokens("at-1", "rt-1", 3600)), (204, String::new())]).await;
        let (p, store) = provider(&url);
        p.verify("sam@example.com", "123456").await.unwrap();
        p.sign_out().await.unwrap();
        assert!(p.signed_in_as().is_none());
        assert!(store.get(&p.refresh_key()).unwrap().is_none());
        assert!(store.get(&p.user_key()).unwrap().is_none());
        assert!(matches!(p.current().await, Err(AuthError::NotSignedIn(_))));
        let seen = seen.lock().unwrap();
        assert!(seen[1].starts_with("POST /v1/auth/email/signout "));
        assert!(seen[1].contains("\"refresh_token\":\"rt-1\""));
    }
}
