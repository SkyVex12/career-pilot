//! Career-OS sign-in (ARCHITECTURE.md, "Career-OS sign-in"). The person signs in on Career-OS's
//! own site, in their browser. Career-OS sends a one-time code back to this computer (a loopback
//! redirect, PKCE). This app gives that code to the Copilot API, which checks Career-OS's signed
//! assertion and issues its own access and refresh tokens, the same ones email sign-in uses.
//! The desktop and the API never see a Career-OS password.
//!
//! The refresh token lives in the OS credential store, beside the Google and email ones. The
//! access token lives in memory only. [`CareerOsTokenProvider`] is a [`TokenProvider`].

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use rand::Rng;
use serde::{Deserialize, Serialize};
use tokio::net::TcpListener;

use crate::auth::{AuthError, BoxFuture, SecretStore, Token, TokenProvider};
use crate::oauth::{self, CareerOsOption};

/// A port of its own, so a Google sign-in on 53682 can still be open.
const CALLBACK_PORT: u16 = 53683;
const EXPIRY_MARGIN: Duration = Duration::from_secs(120);
const JUST_REFRESHED: Duration = Duration::from_secs(10);
const HTTP_TIMEOUT: Duration = Duration::from_secs(20);

/// Where the stage's API is right now. The email sign-in already has this; Career-OS uses it too.
pub use crate::emailauth::ApiBase;

/// Why a step failed. `code` is the API's, or `not_offered`, `access_denied`, `network`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct CareerOsFailure {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after_s: Option<u64>,
}

impl CareerOsFailure {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self { code: code.into(), message: message.into(), retry_after_s: None }
    }

    fn network(what: impl std::fmt::Display) -> Self {
        Self::new("network", format!("Could not reach the server: {what}"))
    }
}

/// One account on the choice screen: a masked email, how that account signs in, when it was made.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AccountChoice {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(default)]
    pub methods: Vec<String>,
    #[serde(default)]
    pub created_at: String,
}

/// What `POST /auth/career-os/sign-in` and `POST /auth/career-os/complete` answer.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum CareerOsOutcome {
    /// Signed in with Career-OS. `account` is `existing` or `new`.
    SignedIn { name: String, account: String },
    /// Career-OS was added to the account the caller was already signed in to.
    Added,
    /// Case 3: several accounts are linked to this Career-OS account. The person picks one
    /// and proves it by signing in to it with its own method.
    ChooseAccount { ticket: String, expires_in_s: u64, accounts: Vec<AccountChoice> },
    /// Case 4: no link, but the email matches an account. `verified` is false when Career-OS
    /// did not verify the address, and then `account` is absent (the email is only a hint).
    ExistingOrNew {
        ticket: String,
        expires_in_s: u64,
        verified: bool,
        #[serde(default)]
        account: Option<AccountChoice>,
    },
}

impl CareerOsOutcome {
    /// The name to show, when this outcome signed the user in by itself.
    pub fn name(&self) -> Option<&str> {
        match self {
            Self::SignedIn { name, .. } => Some(name),
            _ => None,
        }
    }
}

/// The code the browser brought back, plus what the API needs to redeem it.
pub struct BrowserCode {
    code: String,
    verifier: String,
    redirect_uri: String,
}

/// The authorize page URL: Career-OS's page, with PKCE and the loopback redirect.
pub fn authorize_url(option: &CareerOsOption, redirect: &str, challenge: &str, state: &str) -> String {
    let base = option.authorize_url.trim_end_matches('/');
    // The page is `/career-pilot/authorize`; the site URL may already include that path.
    let page = if base.ends_with("/career-pilot/authorize") {
        base.to_string()
    } else {
        format!("{base}/career-pilot/authorize")
    };
    format!(
        "{page}?client_id={}&redirect_uri={}&code_challenge={}&code_challenge_method=S256&state={}",
        oauth::encode(&option.client_id),
        oauth::encode(redirect),
        oauth::encode(challenge),
        oauth::encode(state),
    )
}

/// Open the browser and wait for Career-OS's redirect. Nothing is sent to the API yet.
pub async fn browser_code(
    option: &CareerOsOption,
    open: impl FnOnce(&str) -> Result<(), String>,
) -> Result<BrowserCode, CareerOsFailure> {
    let listener = TcpListener::bind(("127.0.0.1", CALLBACK_PORT)).await.map_err(|e| {
        CareerOsFailure::new(
            "unavailable",
            format!("port {CALLBACK_PORT} is busy ({e}); close the other sign-in and retry"),
        )
    })?;
    let pkce = oauth::Pkce::new();
    let state = random_state();
    let redirect = oauth::redirect_uri(CALLBACK_PORT);
    let url = authorize_url(option, &redirect, &pkce.challenge, &state);
    open(&url).map_err(|e| CareerOsFailure::new("unavailable", e))?;
    let code = tokio::time::timeout(oauth::SIGN_IN_TIMEOUT, oauth::wait_for_code(listener, &state))
        .await
        .map_err(|_| {
            CareerOsFailure::new(
                "career_os_unavailable",
                "HubRuby didn't finish sign-in. Google and email still work.",
            )
        })?
        .map_err(from_auth)?;
    Ok(BrowserCode { code, verifier: pkce.verifier, redirect_uri: redirect })
}

fn random_state() -> String {
    let mut bytes = [0u8; 24];
    rand::rng().fill(&mut bytes[..]);
    base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, bytes)
}

fn from_auth(e: AuthError) -> CareerOsFailure {
    let message = e.to_string();
    if message.contains("access_denied") {
        CareerOsFailure::new("access_denied", "You cancelled sign-in on HubRuby.")
    } else {
        CareerOsFailure::new("unavailable", message)
    }
}

#[derive(Debug, Deserialize)]
struct Problem {
    detail: serde_json::Value,
}

fn failure(status: u16, body: &str) -> CareerOsFailure {
    let detail = serde_json::from_str::<Problem>(body).map(|p| p.detail).unwrap_or_default();
    if let Ok(mut f) = serde_json::from_value::<CareerOsFailure>(detail.clone()) {
        if f.code.is_empty() {
            f.code = "unavailable".into();
        }
        return f;
    }
    if status == 404 {
        return CareerOsFailure::new("not_offered", "This server does not offer HubRuby sign-in.");
    }
    let text = detail.as_str().map(str::to_string).unwrap_or_else(|| format!("HTTP {status}"));
    CareerOsFailure::new("unavailable", text)
}

#[derive(Debug, Deserialize)]
struct TokenBody {
    access_token: String,
    refresh_token: String,
    expires_in: u64,
    #[serde(default)]
    name: String,
}

struct Cached {
    token: Token,
    at: Instant,
}

pub struct CareerOsTokenProvider {
    stage: String,
    base: Arc<dyn ApiBase>,
    store: Arc<dyn SecretStore>,
    http: reqwest::Client,
    cached: tokio::sync::Mutex<Option<Cached>>,
}

impl CareerOsTokenProvider {
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
        format!("career-os-refresh:{}", self.stage)
    }

    fn user_key(&self) -> String {
        format!("career-os-user:{}", self.stage)
    }

    /// Who is signed in with Career-OS, if anyone: the name saved at sign-in.
    pub fn signed_in_as(&self) -> Option<String> {
        match self.store.get(&self.refresh_key()) {
            Ok(Some(_)) => Some(self.store.get(&self.user_key()).ok().flatten().unwrap_or_default()),
            _ => None,
        }
    }

    async fn post(
        &self,
        path: &str,
        body: serde_json::Value,
        bearer: Option<&str>,
    ) -> Result<String, CareerOsFailure> {
        let base = self.base.api_url().await.map_err(CareerOsFailure::network)?;
        let url = format!("{}/auth/{path}", base.trim_end_matches('/'));
        let mut request = self.http.post(&url).json(&body);
        if let Some(token) = bearer {
            request = request.bearer_auth(token);
        }
        let response = request.send().await.map_err(CareerOsFailure::network)?;
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        if status.is_success() { Ok(text) } else { Err(failure(status.as_u16(), &text)) }
    }

    /// The browser's code for a sign-in, or for a choice. `bearer`: the caller is already
    /// signed in another way, and this adds Career-OS to that account.
    pub async fn exchange(
        &self,
        pending: &BrowserCode,
        bearer: Option<&str>,
    ) -> Result<CareerOsOutcome, CareerOsFailure> {
        let text = self
            .post(
                "career-os/sign-in",
                serde_json::json!({
                    "code": pending.code,
                    "code_verifier": pending.verifier,
                    "redirect_uri": pending.redirect_uri,
                }),
                bearer,
            )
            .await?;
        self.keep(&text).await
    }

    /// The person's answer to case 3 or 4. `choice` `existing` needs `bearer`: a sign-in to
    /// that account with its own method. `new` makes a separate account.
    pub async fn complete(
        &self,
        ticket: &str,
        choice: &str,
        account_id: Option<&str>,
        bearer: Option<&str>,
    ) -> Result<CareerOsOutcome, CareerOsFailure> {
        let mut body = serde_json::json!({ "ticket": ticket, "choice": choice });
        if let Some(id) = account_id {
            body["account_id"] = serde_json::Value::String(id.to_string());
        }
        let text = self.post("career-os/complete", body, bearer).await?;
        self.keep(&text).await
    }

    /// A `signed_in` answer is stored. `added` and the two questions are not: the app stays
    /// signed in the way it already was, or it is not signed in yet.
    async fn keep(&self, text: &str) -> Result<CareerOsOutcome, CareerOsFailure> {
        let value: serde_json::Value =
            serde_json::from_str(text).map_err(|e| CareerOsFailure::new("unavailable", e.to_string()))?;
        if value.get("status").and_then(|s| s.as_str()) == Some("signed_in") {
            let tokens: TokenBody = serde_json::from_value(value.clone())
                .map_err(|e| CareerOsFailure::new("unavailable", e.to_string()))?;
            let name =
                if tokens.name.is_empty() { "HubRuby account".to_string() } else { tokens.name.clone() };
            self.store
                .set(&self.refresh_key(), &tokens.refresh_token)
                .and_then(|()| self.store.set(&self.user_key(), &name))
                .map_err(|e| CareerOsFailure::new("unavailable", e.to_string()))?;
            *self.cached.lock().await = Some(Cached { token: token_of(&tokens), at: Instant::now() });
        }
        serde_json::from_value(value).map_err(|e| CareerOsFailure::new("unavailable", e.to_string()))
    }

    pub async fn sign_out(&self) -> Result<(), AuthError> {
        let refresh = self.store.get(&self.refresh_key()).ok().flatten();
        *self.cached.lock().await = None;
        self.store.delete(&self.refresh_key())?;
        self.store.delete(&self.user_key())?;
        if let Some(refresh) = refresh {
            let _ = self.post("token/signout", serde_json::json!({ "refresh_token": refresh }), None).await;
        }
        Ok(())
    }

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
        match self.post("token/refresh", serde_json::json!({ "refresh_token": refresh }), None).await {
            Ok(text) => {
                let tokens: TokenBody = serde_json::from_str(&text)
                    .map_err(|e| AuthError::NotSignedIn(format!("token response: {e}")))?;
                self.store.set(&self.refresh_key(), &tokens.refresh_token)?;
                let token = token_of(&tokens);
                *cached = Some(Cached { token: token.clone(), at: Instant::now() });
                Ok(token)
            }
            Err(f) if f.code == "invalid_refresh_token" => {
                let _ = self.store.delete(&self.refresh_key());
                *cached = None;
                Err(AuthError::NotSignedIn("the sign-in expired; sign in again under Settings".into()))
            }
            Err(f) => Err(AuthError::NotSignedIn(f.message)),
        }
    }
}

fn token_of(tokens: &TokenBody) -> Token {
    Token {
        access_token: tokens.access_token.clone(),
        expires_at: Some(SystemTime::now() + Duration::from_secs(tokens.expires_in)),
    }
}

impl TokenProvider for CareerOsTokenProvider {
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
    use crate::emailauth::FixedApi;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    async fn fake_api(replies: Vec<(u16, String)>) -> (String, Arc<std::sync::Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let seen2 = seen.clone();
        tokio::spawn(async move {
            for (status, body) in replies {
                let (mut s, _) = listener.accept().await.unwrap();
                let mut buf = vec![0u8; 16384];
                let n = s.read(&mut buf).await.unwrap();
                seen2.lock().unwrap().push(String::from_utf8_lossy(&buf[..n]).into_owned());
                let response = format!(
                    "HTTP/1.1 {status} X\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = s.write_all(response.as_bytes()).await;
            }
        });
        (format!("http://{addr}/v1"), seen)
    }

    fn option() -> CareerOsOption {
        CareerOsOption { authorize_url: "https://career-os.example".into(), client_id: "career-pilot".into() }
    }

    #[test]
    fn the_authorize_url_is_the_career_os_page_with_pkce() {
        let url = authorize_url(&option(), "http://127.0.0.1:53683/callback", "chal", "st");
        assert!(url.starts_with("https://career-os.example/career-pilot/authorize?"));
        assert!(url.contains("client_id=career-pilot"));
        assert!(url.contains("redirect_uri=http%3A%2F%2F127.0.0.1%3A53683%2Fcallback"));
        assert!(url.contains("code_challenge=chal"));
        assert!(url.contains("code_challenge_method=S256"));
        assert!(url.contains("state=st"));
        let again = authorize_url(
            &CareerOsOption {
                authorize_url: "https://career-os.example/career-pilot/authorize".into(),
                client_id: "career-pilot".into(),
            },
            "http://127.0.0.1:53683/callback",
            "chal",
            "st",
        );
        assert!(!again.contains("/career-pilot/authorize/career-pilot/authorize"));
    }

    #[tokio::test]
    async fn a_signed_in_answer_keeps_the_refresh_token() {
        let (base, seen) = fake_api(vec![(
            200,
            r#"{"status":"signed_in","account":"new","access_token":"at","refresh_token":"rt","expires_in":3600,"name":"Sam"}"#.into(),
        )])
        .await;
        let store = Arc::new(MemorySecretStore::default());
        let p = CareerOsTokenProvider::new("stage", Arc::new(FixedApi(base)), store.clone());
        let pending = BrowserCode {
            code: "c".into(),
            verifier: "v".repeat(43),
            redirect_uri: "http://127.0.0.1:53683/callback".into(),
        };
        let out = p.exchange(&pending, None).await.unwrap();
        assert_eq!(out.name(), Some("Sam"));
        assert_eq!(p.signed_in_as().as_deref(), Some("Sam"));
        assert!(seen.lock().unwrap()[0].contains("POST /v1/auth/career-os/sign-in"));
        assert!(seen.lock().unwrap()[0].contains("\"code_verifier\""));
        let token = p.current().await.unwrap();
        assert_eq!(token.access_token, "at");
    }

    #[tokio::test]
    async fn a_choice_does_not_store_a_token_and_a_refresh_uses_the_shared_endpoint() {
        let (base, seen) = fake_api(vec![
            (
                200,
                r#"{"status":"choose_account","ticket":"tkt","expires_in_s":600,"accounts":[{"id":"a","email":"s•••@x.com","methods":["google"],"created_at":"2026-01-02"}]}"#.into(),
            ),
            (
                200,
                r#"{"status":"signed_in","account":"existing","access_token":"at","refresh_token":"rt","expires_in":0,"name":"Sam"}"#.into(),
            ),
            (200, r#"{"access_token":"at2","refresh_token":"rt2","expires_in":3600,"name":"Sam"}"#.into()),
        ])
        .await;
        let store = Arc::new(MemorySecretStore::default());
        let p = CareerOsTokenProvider::new("stage", Arc::new(FixedApi(base)), store);
        let pending = BrowserCode {
            code: "c".into(),
            verifier: "v".into(),
            redirect_uri: "http://127.0.0.1/callback".into(),
        };
        let choice = p.exchange(&pending, None).await.unwrap();
        assert!(matches!(choice, CareerOsOutcome::ChooseAccount { .. }));
        assert!(p.signed_in_as().is_none());
        let done = p.complete("tkt", "existing", Some("a"), Some("proof")).await.unwrap();
        assert_eq!(done.name(), Some("Sam"));
        let proof = seen.lock().unwrap()[1].to_ascii_lowercase();
        assert!(proof.contains("authorization: bearer proof"), "{proof}");
        assert!(seen.lock().unwrap()[1].contains("/auth/career-os/complete"));
        let _ = p.refresh().await.unwrap();
        assert!(seen.lock().unwrap()[2].contains("/auth/token/refresh"));
        assert!(seen.lock().unwrap()[2].contains("\"refresh_token\":\"rt\""));
    }

    #[tokio::test]
    async fn career_os_being_down_keeps_its_code() {
        let (base, _) = fake_api(vec![(
            503,
            r#"{"detail":{"code":"career_os_unavailable","message":"HubRuby did not answer.","retry_after_s":60}}"#.into(),
        )])
        .await;
        let p = CareerOsTokenProvider::new(
            "stage",
            Arc::new(FixedApi(base)),
            Arc::new(MemorySecretStore::default()),
        );
        let pending = BrowserCode {
            code: "c".into(),
            verifier: "v".into(),
            redirect_uri: "http://127.0.0.1/callback".into(),
        };
        let err = p.exchange(&pending, None).await.unwrap_err();
        assert_eq!(err.code, "career_os_unavailable");
        assert_eq!(err.retry_after_s, Some(60));
        assert!(p.signed_in_as().is_none());
    }
}
