//! Browser sign-in (ARCHITECTURE.md, "Desktop client": Login): OAuth 2.0 with PKCE in the
//! user's browser, the tokens back through a loopback redirect, and the refresh token in the
//! OS credential store (Windows Credential Manager). The bearer token lives in memory only.
//! Two providers speak it: Cognito on AWS, and Google on GCP (Stage 0), where the services take
//! Google's ID token as the bearer (its access tokens are opaque) and the desktop client's
//! secret, which Google does not treat as confidential, goes with each token request.
//!
//! [`OAuthTokenProvider`] is a [`TokenProvider`], so the gateway client and the API client
//! use it exactly as they used the dev token: the gateway client sends `auth.refresh` with a
//! new token before the old one expires (and on `auth.expiring`), and the API client refreshes
//! once on a `401`.
//!
//! The app learns the provider, its endpoints and the client from `GET /v1/auth/config`, so a
//! deployment's sign-in can change without a new build.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use crate::auth::{AuthError, BoxFuture, SecretStore, Token, TokenProvider};

/// The loopback port the provider redirects to: Cognito's app client lists exactly this
/// redirect (infra/modules/cognito), and Google's desktop clients accept any loopback port.
pub const CALLBACK_PORT: u16 = 53682;
/// How long the browser sign-in may take.
pub const SIGN_IN_TIMEOUT: Duration = Duration::from_secs(300);
/// An access token this close to its expiry is refreshed before use.
const EXPIRY_MARGIN: Duration = Duration::from_secs(120);
const HTTP_TIMEOUT: Duration = Duration::from_secs(20);

pub fn redirect_uri(port: u16) -> String {
    format!("http://127.0.0.1:{port}/callback")
}

/// `GET /v1/auth/config`: Cognito, Google, or the dev token on the local stack.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(default)]
pub struct AuthConfig {
    pub provider: String,
    /// Cognito: the user pool's domain, with /oauth2/authorize, /oauth2/token and
    /// /oauth2/revoke under it.
    pub domain: Option<String>,
    pub client_id: Option<String>,
    pub scopes: Vec<String>,
    /// Google: the endpoints, the desktop client's secret, the token the services take, and
    /// extra authorize parameters.
    pub authorize_url: Option<String>,
    pub token_url: Option<String>,
    pub revoke_url: Option<String>,
    pub client_secret: Option<String>,
    pub bearer: Option<String>,
    pub authorize_params: BTreeMap<String, String>,
}

/// What the app needs to sign in to one deployment, kept in the settings file. A client
/// secret here is Google's desktop one, which Google does not treat as confidential.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(default)]
pub struct OAuthConfig {
    /// "cognito" or "google"; settings saved before Google was added say nothing (Cognito).
    pub provider: String,
    /// Cognito: https://<prefix>.auth.<region>.amazoncognito.com. Google: its accounts domain.
    pub domain: String,
    pub client_id: String,
    pub scopes: Vec<String>,
    pub authorize_url: Option<String>,
    pub token_url: Option<String>,
    pub revoke_url: Option<String>,
    pub client_secret: Option<String>,
    /// The ID token is the bearer (Google), instead of the access token (Cognito).
    pub id_token_bearer: bool,
    pub authorize_params: BTreeMap<String, String>,
}

impl OAuthConfig {
    pub fn authorize_endpoint(&self) -> String {
        self.authorize_url.clone().unwrap_or_else(|| format!("{}/oauth2/authorize", self.domain))
    }

    pub fn token_endpoint(&self) -> String {
        self.token_url.clone().unwrap_or_else(|| format!("{}/oauth2/token", self.domain))
    }

    pub fn revoke_endpoint(&self) -> String {
        self.revoke_url.clone().unwrap_or_else(|| format!("{}/oauth2/revoke", self.domain))
    }

    /// Who the user signs in with, for messages.
    pub fn provider_name(&self) -> &'static str {
        if self.provider == "google" { "Google" } else { "Cognito" }
    }
}

fn https(url: &Option<String>) -> Option<String> {
    url.as_ref().filter(|u| u.starts_with("https://")).map(|u| u.trim_end_matches('/').to_string())
}

impl AuthConfig {
    pub fn oauth(&self) -> Result<OAuthConfig, AuthError> {
        let scopes = if self.scopes.is_empty() { vec!["openid".into()] } else { self.scopes.clone() };
        match (self.provider.as_str(), &self.client_id) {
            ("cognito", Some(client_id)) if https(&self.domain).is_some() => Ok(OAuthConfig {
                provider: "cognito".into(),
                domain: https(&self.domain).unwrap_or_default(),
                client_id: client_id.clone(),
                scopes,
                ..OAuthConfig::default()
            }),
            ("google", Some(client_id))
                if https(&self.authorize_url).is_some() && https(&self.token_url).is_some() =>
            {
                Ok(OAuthConfig {
                    provider: "google".into(),
                    domain: "https://accounts.google.com".into(),
                    client_id: client_id.clone(),
                    scopes,
                    authorize_url: https(&self.authorize_url),
                    token_url: https(&self.token_url),
                    revoke_url: https(&self.revoke_url),
                    client_secret: self.client_secret.clone(),
                    id_token_bearer: self.bearer.as_deref() == Some("id_token"),
                    authorize_params: self.authorize_params.clone(),
                })
            }
            (p @ ("cognito" | "google"), _) => {
                Err(AuthError::NotSignedIn(format!("the API's {p} sign-in settings are incomplete")))
            }
            (other, _) => Err(AuthError::NotSignedIn(format!(
                "this API signs in with {other}; use the local environment for the dev token"
            ))),
        }
    }
}

/// Fetch the sign-in settings from the API, without a token.
pub async fn fetch_auth_config(api_url: &str) -> Result<AuthConfig, AuthError> {
    let url = format!("{}/auth/config", api_url.trim_end_matches('/'));
    let response = http()
        .get(&url)
        .send()
        .await
        .map_err(|e| AuthError::NotSignedIn(format!("could not reach {url}: {e}")))?;
    if !response.status().is_success() {
        return Err(AuthError::NotSignedIn(format!("{url}: HTTP {}", response.status().as_u16())));
    }
    response.json().await.map_err(|e| AuthError::NotSignedIn(format!("{url}: {e}")))
}

fn http() -> reqwest::Client {
    reqwest::Client::builder().timeout(HTTP_TIMEOUT).build().expect("an HTTP client")
}

/// RFC 7636: a random verifier and its S256 challenge.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pkce {
    pub verifier: String,
    pub challenge: String,
}

impl Pkce {
    pub fn new() -> Self {
        let mut bytes = [0u8; 48];
        rand::rng().fill(&mut bytes[..]);
        Self::from_verifier(URL_SAFE_NO_PAD.encode(bytes))
    }

    pub fn from_verifier(verifier: String) -> Self {
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        Self { verifier, challenge }
    }
}

impl Default for Pkce {
    fn default() -> Self {
        Self::new()
    }
}

fn random_state() -> String {
    let mut bytes = [0u8; 24];
    rand::rng().fill(&mut bytes[..]);
    URL_SAFE_NO_PAD.encode(bytes)
}

fn encode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    let hex = |b: u8| (b as char).to_digit(16).map(|d| d as u8);
    while i < bytes.len() {
        match bytes[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < bytes.len() => match (hex(bytes[i + 1]), hex(bytes[i + 2])) {
                (Some(hi), Some(lo)) => {
                    out.push(hi << 4 | lo);
                    i += 3;
                    continue;
                }
                _ => out.push(b'%'),
            },
            b => out.push(b),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn form(pairs: &[(&str, &str)]) -> String {
    pairs.iter().map(|(k, v)| format!("{}={}", encode(k), encode(v))).collect::<Vec<_>>().join("&")
}

pub fn authorize_url(cfg: &OAuthConfig, pkce: &Pkce, state: &str, redirect: &str) -> String {
    let scope = cfg.scopes.join(" ");
    let mut pairs = vec![
        ("response_type", "code"),
        ("client_id", cfg.client_id.as_str()),
        ("redirect_uri", redirect),
        ("scope", scope.as_str()),
        ("state", state),
        ("code_challenge", pkce.challenge.as_str()),
        ("code_challenge_method", "S256"),
    ];
    pairs.extend(cfg.authorize_params.iter().map(|(k, v)| (k.as_str(), v.as_str())));
    format!("{}?{}", cfg.authorize_endpoint(), form(&pairs))
}

/// The query of the redirect's request line, if it is the callback.
fn callback_query(request_line: &str) -> Option<Vec<(String, String)>> {
    let target = request_line.split_whitespace().nth(1)?;
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if path != "/callback" {
        return None;
    }
    Some(
        query
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| {
                let (k, v) = p.split_once('=').unwrap_or((p, ""));
                (decode(k), decode(v))
            })
            .collect(),
    )
}

const PAGE_DONE: &str = "<!doctype html><meta charset=utf-8><title>Copilot</title>\
<body style=\"font-family:system-ui;margin:3em\"><h2>Signed in to Copilot</h2><p>You can close this tab.</p>";
const PAGE_FAILED: &str = "<!doctype html><meta charset=utf-8><title>Copilot</title>\
<body style=\"font-family:system-ui;margin:3em\"><h2>Sign-in did not finish</h2><p>Go back to Copilot and try again.</p>";

async fn respond(stream: &mut tokio::net::TcpStream, status: &str, body: &str) {
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\
         Cache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes()).await;
    let _ = stream.shutdown().await;
}

/// Serve the loopback redirect until the callback with our `state` arrives; returns the code.
async fn wait_for_code(listener: TcpListener, state: &str) -> Result<String, AuthError> {
    loop {
        let (mut stream, _) = listener.accept().await.map_err(|e| AuthError::NotSignedIn(e.to_string()))?;
        let mut buf = vec![0u8; 8192];
        let n = match tokio::time::timeout(Duration::from_secs(10), stream.read(&mut buf)).await {
            Ok(Ok(n)) => n,
            _ => continue,
        };
        let text = String::from_utf8_lossy(&buf[..n]);
        let Some(params) = text.lines().next().and_then(callback_query) else {
            respond(&mut stream, "404 Not Found", "").await;
            continue; // the browser's favicon request, or anything else
        };
        let get = |k: &str| params.iter().find(|(key, _)| key == k).map(|(_, v)| v.clone());
        if get("state").as_deref() != Some(state) {
            // Not the sign-in this app started: ignore it and keep waiting.
            respond(&mut stream, "400 Bad Request", PAGE_FAILED).await;
            continue;
        }
        if let Some(error) = get("error") {
            respond(&mut stream, "200 OK", PAGE_FAILED).await;
            let detail = get("error_description").unwrap_or_default();
            return Err(AuthError::NotSignedIn(format!("sign-in failed: {error} {detail}").trim().into()));
        }
        let Some(code) = get("code") else {
            respond(&mut stream, "400 Bad Request", PAGE_FAILED).await;
            return Err(AuthError::NotSignedIn("the sign-in redirect had no code".into()));
        };
        respond(&mut stream, "200 OK", PAGE_DONE).await;
        return Ok(code);
    }
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: String,
    #[serde(default)]
    id_token: Option<String>,
    #[serde(default)]
    refresh_token: Option<String>,
    expires_in: u64,
}

#[derive(Debug, Deserialize)]
struct TokenErrorBody {
    error: String,
}

/// The email in an ID token, for showing who is signed in. Not verified: the services verify
/// the access token; this is display only.
fn email_of(id_token: &str) -> Option<String> {
    let payload = id_token.split('.').nth(1)?;
    let claims: serde_json::Value = serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload).ok()?).ok()?;
    claims.get("email")?.as_str().map(str::to_string)
}

pub struct OAuthTokenProvider {
    cfg: OAuthConfig,
    store: Arc<dyn SecretStore>,
    http: reqwest::Client,
    cached: tokio::sync::Mutex<Option<Token>>,
    port: u16,
}

impl OAuthTokenProvider {
    pub fn new(cfg: OAuthConfig, store: Arc<dyn SecretStore>) -> Self {
        Self::with_port(cfg, store, CALLBACK_PORT)
    }

    /// Tests use another port.
    pub fn with_port(cfg: OAuthConfig, store: Arc<dyn SecretStore>, port: u16) -> Self {
        Self { cfg, store, http: http(), cached: tokio::sync::Mutex::new(None), port }
    }

    pub fn config(&self) -> &OAuthConfig {
        &self.cfg
    }

    fn refresh_key(&self) -> String {
        format!("cognito-refresh:{}", self.cfg.client_id)
    }

    fn user_key(&self) -> String {
        format!("cognito-user:{}", self.cfg.client_id)
    }

    /// Who is signed in, if anyone: the email saved at sign-in.
    pub fn signed_in_as(&self) -> Option<String> {
        match self.store.get(&self.refresh_key()) {
            Ok(Some(_)) => Some(self.store.get(&self.user_key()).ok().flatten().unwrap_or_default()),
            _ => None,
        }
    }

    /// The browser sign-in: `open` shows the URL in the user's browser; this waits for the
    /// redirect, exchanges the code and keeps the refresh token. Returns the user's email.
    pub async fn sign_in(&self, open: impl FnOnce(&str) -> Result<(), String>) -> Result<String, AuthError> {
        let listener = TcpListener::bind(("127.0.0.1", self.port)).await.map_err(|e| {
            AuthError::NotSignedIn(format!(
                "port {} is busy ({e}); close the other sign-in and retry",
                self.port
            ))
        })?;
        let pkce = Pkce::new();
        let state = random_state();
        let redirect = redirect_uri(self.port);
        open(&authorize_url(&self.cfg, &pkce, &state, &redirect)).map_err(AuthError::NotSignedIn)?;
        let code = tokio::time::timeout(SIGN_IN_TIMEOUT, wait_for_code(listener, &state))
            .await
            .map_err(|_| AuthError::NotSignedIn("sign-in timed out; try again".into()))??;
        let tokens = self
            .token_request(&[
                ("grant_type", "authorization_code"),
                ("code", &code),
                ("redirect_uri", &redirect),
                ("code_verifier", &pkce.verifier),
            ])
            .await?;
        let refresh = tokens.refresh_token.clone().ok_or_else(|| {
            AuthError::NotSignedIn(format!("{} returned no refresh token", self.cfg.provider_name()))
        })?;
        let email = tokens.id_token.as_deref().and_then(email_of).unwrap_or_default();
        let token = self.token_of(&tokens)?;
        self.store.set(&self.refresh_key(), &refresh)?;
        self.store.set(&self.user_key(), &email)?;
        *self.cached.lock().await = Some(token);
        Ok(email)
    }

    /// Forget the tokens here and revoke the refresh token at the provider (best effort).
    pub async fn sign_out(&self) -> Result<(), AuthError> {
        if let Ok(Some(refresh)) = self.store.get(&self.refresh_key()) {
            let body = form(&[("token", &refresh), ("client_id", &self.cfg.client_id)]);
            let _ = self
                .http
                .post(self.cfg.revoke_endpoint())
                .header("Content-Type", "application/x-www-form-urlencoded")
                .body(body)
                .send()
                .await;
        }
        *self.cached.lock().await = None;
        self.store.delete(&self.refresh_key())?;
        self.store.delete(&self.user_key())
    }

    /// The bearer the services take: the access token (Cognito) or the ID token (Google).
    fn token_of(&self, tokens: &TokenResponse) -> Result<Token, AuthError> {
        let bearer = if self.cfg.id_token_bearer {
            tokens.id_token.clone().ok_or_else(|| {
                AuthError::NotSignedIn(format!("{} returned no ID token", self.cfg.provider_name()))
            })?
        } else {
            tokens.access_token.clone()
        };
        Ok(Token {
            access_token: bearer,
            expires_at: Some(SystemTime::now() + Duration::from_secs(tokens.expires_in)),
        })
    }

    async fn token_request(&self, pairs: &[(&str, &str)]) -> Result<TokenResponse, AuthError> {
        let mut all = vec![("client_id", self.cfg.client_id.as_str())];
        if let Some(secret) = &self.cfg.client_secret {
            all.push(("client_secret", secret.as_str()));
        }
        all.extend_from_slice(pairs);
        let name = self.cfg.provider_name();
        let response = self
            .http
            .post(self.cfg.token_endpoint())
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(form(&all))
            .send()
            .await
            .map_err(|e| AuthError::NotSignedIn(format!("could not reach {name}: {e}")))?;
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        if status.is_success() {
            return serde_json::from_str(&text)
                .map_err(|e| AuthError::NotSignedIn(format!("token response: {e}")));
        }
        let error = serde_json::from_str::<TokenErrorBody>(&text).map(|b| b.error).unwrap_or_default();
        Err(AuthError::NotSignedIn(format!(
            "{name} refused the token request: HTTP {} {error}",
            status.as_u16()
        )))
    }

    async fn refresh_now(&self) -> Result<Token, AuthError> {
        let Some(refresh) = self.store.get(&self.refresh_key())? else {
            return Err(AuthError::NotSignedIn("sign in under Settings".into()));
        };
        match self.token_request(&[("grant_type", "refresh_token"), ("refresh_token", &refresh)]).await {
            Ok(tokens) => {
                // Cognito returns a new refresh token only when rotation is on; Google never.
                if let Some(rotated) = &tokens.refresh_token {
                    self.store.set(&self.refresh_key(), rotated)?;
                }
                let token = self.token_of(&tokens)?;
                *self.cached.lock().await = Some(token.clone());
                Ok(token)
            }
            Err(AuthError::NotSignedIn(msg)) if msg.contains("invalid_grant") => {
                // Expired or revoked (the user was deleted or signed out elsewhere).
                let _ = self.store.delete(&self.refresh_key());
                *self.cached.lock().await = None;
                Err(AuthError::NotSignedIn("the sign-in expired; sign in again under Settings".into()))
            }
            Err(e) => Err(e),
        }
    }
}

impl TokenProvider for OAuthTokenProvider {
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move {
            if let Some(token) = self.cached.lock().await.clone()
                && token.expires_at.is_some_and(|at| at > SystemTime::now() + EXPIRY_MARGIN)
            {
                return Ok(token);
            }
            self.refresh_now().await
        })
    }

    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(self.refresh_now())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::MemorySecretStore;

    fn cfg(domain: &str) -> OAuthConfig {
        OAuthConfig {
            provider: "cognito".into(),
            domain: domain.into(),
            client_id: "client-1".into(),
            scopes: vec!["openid".into(), "email".into()],
            ..OAuthConfig::default()
        }
    }

    #[test]
    fn pkce_matches_rfc_7636() {
        let p = Pkce::from_verifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk".into());
        assert_eq!(p.challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
        let fresh = Pkce::new();
        assert!(fresh.verifier.len() >= 43 && fresh.verifier.len() <= 128);
        assert_ne!(fresh, Pkce::new());
    }

    #[test]
    fn authorize_url_carries_the_challenge_and_state() {
        let p = Pkce::from_verifier("v".repeat(43));
        let url = authorize_url(
            &cfg("https://x.auth.us-east-1.amazoncognito.com"),
            &p,
            "st8",
            &redirect_uri(53682),
        );
        assert!(
            url.starts_with(
                "https://x.auth.us-east-1.amazoncognito.com/oauth2/authorize?response_type=code&"
            )
        );
        assert!(url.contains("&redirect_uri=http%3A%2F%2F127.0.0.1%3A53682%2Fcallback&"));
        assert!(url.contains("&scope=openid%20email&state=st8&"));
        assert!(url.ends_with(&format!("&code_challenge={}&code_challenge_method=S256", p.challenge)));
    }

    #[test]
    fn the_callback_query_is_parsed() {
        let q = callback_query("GET /callback?code=abc%2D1&state=x%20y HTTP/1.1").unwrap();
        assert_eq!(q, vec![("code".into(), "abc-1".into()), ("state".into(), "x y".into())]);
        assert!(callback_query("GET /favicon.ico HTTP/1.1").is_none());
    }

    #[test]
    fn auth_config_must_be_cognito_or_google_over_https() {
        let ok = AuthConfig {
            provider: "cognito".into(),
            domain: Some("https://d.auth.us-east-1.amazoncognito.com/".into()),
            client_id: Some("c".into()),
            ..AuthConfig::default()
        };
        let cfg = ok.oauth().unwrap();
        assert_eq!(cfg.domain, "https://d.auth.us-east-1.amazoncognito.com");
        assert_eq!(cfg.token_endpoint(), "https://d.auth.us-east-1.amazoncognito.com/oauth2/token");
        assert!(!cfg.id_token_bearer && cfg.client_secret.is_none());
        let dev = AuthConfig { provider: "dev".into(), ..AuthConfig::default() };
        assert!(dev.oauth().is_err());
        let plain = AuthConfig { domain: Some("http://d".into()), ..ok };
        assert!(plain.oauth().is_err());

        // What GET /v1/auth/config returns on GCP (copilot_api.routes.auth).
        let google: AuthConfig = serde_json::from_str(
            r#"{"provider":"google","client_id":"123-abc.apps.googleusercontent.com",
                "client_secret":"GOCSPX-x","scopes":["openid","email","profile"],
                "authorize_url":"https://accounts.google.com/o/oauth2/v2/auth",
                "token_url":"https://oauth2.googleapis.com/token",
                "revoke_url":"https://oauth2.googleapis.com/revoke","bearer":"id_token",
                "authorize_params":{"access_type":"offline","prompt":"consent"}}"#,
        )
        .unwrap();
        let g = google.oauth().unwrap();
        assert!(g.id_token_bearer);
        assert_eq!(g.token_endpoint(), "https://oauth2.googleapis.com/token");
        assert_eq!(g.revoke_endpoint(), "https://oauth2.googleapis.com/revoke");
        let url = authorize_url(&g, &Pkce::from_verifier("v".repeat(43)), "s", &redirect_uri(53682));
        assert!(url.starts_with("https://accounts.google.com/o/oauth2/v2/auth?response_type=code&"));
        assert!(url.ends_with("&access_type=offline&prompt=consent"));
        let no_token_url = AuthConfig { token_url: None, ..google };
        assert!(no_token_url.oauth().is_err());
        // Settings saved before Google was added still load, as Cognito.
        let old: OAuthConfig =
            serde_json::from_str(r#"{"domain":"https://d","client_id":"c","scopes":["openid"]}"#).unwrap();
        assert_eq!(old.authorize_endpoint(), "https://d/oauth2/authorize");
        assert_eq!(old.provider_name(), "Cognito");
    }

    /// A stand-in for the provider's token endpoint: answers each POST with the next canned reply
    /// and records the request bodies.
    async fn fake_token_endpoint(
        replies: Vec<(u16, String)>,
    ) -> (String, Arc<std::sync::Mutex<Vec<String>>>) {
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
                            seen2.lock().unwrap().push(rest.to_string());
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
        (format!("http://{addr}"), seen)
    }

    fn id_token(email: &str) -> String {
        let payload = URL_SAFE_NO_PAD.encode(format!("{{\"email\":\"{email}\"}}"));
        format!("eyJhbGciOiJub25lIn0.{payload}.sig")
    }

    #[tokio::test]
    async fn sign_in_refresh_and_expiry() {
        let tokens = format!(
            "{{\"access_token\":\"at-1\",\"id_token\":\"{}\",\"refresh_token\":\"rt-1\",\"expires_in\":3600,\"token_type\":\"Bearer\"}}",
            id_token("alex@example.com")
        );
        let refreshed =
            "{\"access_token\":\"at-2\",\"expires_in\":3600,\"token_type\":\"Bearer\"}".to_string();
        let (domain, seen) = fake_token_endpoint(vec![
            (200, tokens),
            (200, refreshed),
            (400, "{\"error\":\"invalid_grant\"}".into()),
        ])
        .await;
        let store = Arc::new(MemorySecretStore::default());
        let probe = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = probe.local_addr().unwrap().port();
        drop(probe);
        let p = OAuthTokenProvider::with_port(cfg(&domain), store.clone(), port);
        assert!(p.signed_in_as().is_none());
        assert!(matches!(p.current().await, Err(AuthError::NotSignedIn(_))));

        // The "browser": follow the authorize URL's state back to the loopback redirect.
        let email = p
            .sign_in(|url| {
                let state = url.split("&state=").nth(1).unwrap().split('&').next().unwrap().to_string();
                tokio::spawn(async move {
                    let redirect = format!("http://127.0.0.1:{port}/callback?code=the-code&state={state}");
                    let page = reqwest::get(redirect).await.unwrap().text().await.unwrap();
                    assert!(page.contains("Signed in"));
                });
                Ok(())
            })
            .await
            .unwrap();
        assert_eq!(email, "alex@example.com");
        assert_eq!(p.signed_in_as().as_deref(), Some("alex@example.com"));
        assert_eq!(store.get("cognito-refresh:client-1").unwrap().as_deref(), Some("rt-1"));
        assert_eq!(p.current().await.unwrap().access_token, "at-1");
        let exchange = seen.lock().unwrap()[0].clone();
        assert!(exchange.contains("grant_type=authorization_code") && exchange.contains("code=the-code"));
        assert!(exchange.contains("code_verifier="));

        // The gateway client asks for a fresh token before expiry or after a 4401.
        assert_eq!(p.refresh().await.unwrap().access_token, "at-2");
        assert!(seen.lock().unwrap()[1].contains("refresh_token=rt-1"));
        assert_eq!(store.get("cognito-refresh:client-1").unwrap().as_deref(), Some("rt-1"));

        // A revoked or expired refresh token signs the user out.
        assert!(matches!(p.refresh().await, Err(AuthError::NotSignedIn(m)) if m.contains("sign in again")));
        assert!(p.signed_in_as().is_none());
    }

    #[tokio::test]
    async fn google_sends_its_client_secret_and_uses_the_id_token() {
        let first = format!(
            "{{\"access_token\":\"ya29.opaque\",\"id_token\":\"{}\",\"refresh_token\":\"1//rt\",\"expires_in\":3599}}",
            id_token("sam@gmail.com")
        );
        // Google's refresh answer: a new ID token, no new refresh token.
        let second = format!(
            "{{\"access_token\":\"ya29.opaque2\",\"id_token\":\"{}\",\"expires_in\":3599}}",
            id_token("sam@gmail.com")
        );
        let (base, seen) = fake_token_endpoint(vec![(200, first), (200, second)]).await;
        let store = Arc::new(MemorySecretStore::default());
        let probe = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = probe.local_addr().unwrap().port();
        drop(probe);
        let cfg = OAuthConfig {
            provider: "google".into(),
            domain: "https://accounts.google.com".into(),
            client_id: "123-abc.apps.googleusercontent.com".into(),
            scopes: vec!["openid".into(), "email".into()],
            authorize_url: Some(format!("{base}/auth")),
            token_url: Some(format!("{base}/token")),
            client_secret: Some("GOCSPX-desktop".into()),
            id_token_bearer: true,
            ..OAuthConfig::default()
        };
        let p = OAuthTokenProvider::with_port(cfg, store.clone(), port);
        let email = p
            .sign_in(|url| {
                assert!(url.starts_with(&format!("{base}/auth?response_type=code&")));
                let state = url.split("&state=").nth(1).unwrap().split('&').next().unwrap().to_string();
                tokio::spawn(async move {
                    reqwest::get(format!("http://127.0.0.1:{port}/callback?code=c&state={state}"))
                        .await
                        .unwrap();
                });
                Ok(())
            })
            .await
            .unwrap();
        assert_eq!(email, "sam@gmail.com");
        let bearer = p.current().await.unwrap().access_token;
        assert!(bearer.starts_with("eyJ"), "the ID token is the bearer, not the opaque access token");
        assert!(seen.lock().unwrap()[0].contains("client_secret=GOCSPX-desktop"));
        assert!(p.refresh().await.unwrap().access_token.starts_with("eyJ"));
        assert!(seen.lock().unwrap()[1].contains("client_secret=GOCSPX-desktop"));
        assert_eq!(
            store.get("cognito-refresh:123-abc.apps.googleusercontent.com").unwrap().as_deref(),
            Some("1//rt")
        );
    }

    #[tokio::test]
    async fn a_redirect_with_another_state_is_ignored() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let wait = tokio::spawn(async move { wait_for_code(listener, "mine").await });
        let base = format!("http://127.0.0.1:{port}");
        assert_eq!(reqwest::get(format!("{base}/callback?code=x&state=other")).await.unwrap().status(), 400);
        assert_eq!(reqwest::get(format!("{base}/favicon.ico")).await.unwrap().status(), 404);
        reqwest::get(format!("{base}/callback?error=access_denied&state=mine")).await.unwrap();
        assert!(matches!(wait.await.unwrap(), Err(AuthError::NotSignedIn(m)) if m.contains("access_denied")));
    }
}
