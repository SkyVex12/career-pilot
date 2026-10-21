//! Access tokens for the gateway (ARCHITECTURE.md, "Connection and auth").
//!
//! The client only sees [`TokenProvider`]: a token for the upgrade request, and a fresh one
//! on `auth.expiring`, before expiry, and after a `4401`. Locally, [`DevTokenProvider`]
//! hands out the gateway's static dev token, kept in the OS credential store (Windows
//! Credential Manager) rather than in the settings file. [`crate::oauth`] is the browser
//! sign-in for a deployed stage (Google or Cognito: OAuth with PKCE, refresh token in the
//! credential store) behind the same trait; nothing else changes.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Token {
    pub access_token: String,
    /// None for tokens that do not expire (the dev token).
    pub expires_at: Option<SystemTime>,
}

#[derive(Debug, Clone, thiserror::Error, PartialEq, Eq)]
pub enum AuthError {
    #[error("not signed in: {0}")]
    NotSignedIn(String),
    #[error("credential store: {0}")]
    Store(String),
}

pub trait TokenProvider: Send + Sync {
    /// The token to connect with.
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>>;
    /// A new token: before the current one expires, or after the gateway rejected it.
    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>>;
}

/// Where secrets live. The OS store in the app, memory in tests.
pub trait SecretStore: Send + Sync {
    fn get(&self, key: &str) -> Result<Option<String>, AuthError>;
    fn set(&self, key: &str, value: &str) -> Result<(), AuthError>;
    fn delete(&self, key: &str) -> Result<(), AuthError>;
}

/// Windows Credential Manager (and the macOS Keychain later), through `keyring`.
pub struct OsSecretStore {
    service: String,
}

impl OsSecretStore {
    pub fn new(service: impl Into<String>) -> Self {
        Self { service: service.into() }
    }

    fn entry(&self, key: &str) -> Result<keyring::Entry, AuthError> {
        keyring::Entry::new(&self.service, key).map_err(|e| AuthError::Store(e.to_string()))
    }
}

impl SecretStore for OsSecretStore {
    fn get(&self, key: &str) -> Result<Option<String>, AuthError> {
        match self.entry(key)?.get_password() {
            Ok(v) => Ok(Some(v)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(AuthError::Store(e.to_string())),
        }
    }

    fn set(&self, key: &str, value: &str) -> Result<(), AuthError> {
        self.entry(key)?.set_password(value).map_err(|e| AuthError::Store(e.to_string()))
    }

    fn delete(&self, key: &str) -> Result<(), AuthError> {
        match self.entry(key)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(AuthError::Store(e.to_string())),
        }
    }
}

#[derive(Default)]
pub struct MemorySecretStore(Mutex<std::collections::HashMap<String, String>>);

impl SecretStore for MemorySecretStore {
    fn get(&self, key: &str) -> Result<Option<String>, AuthError> {
        Ok(self.0.lock().unwrap().get(key).cloned())
    }

    fn set(&self, key: &str, value: &str) -> Result<(), AuthError> {
        self.0.lock().unwrap().insert(key.into(), value.into());
        Ok(())
    }

    fn delete(&self, key: &str) -> Result<(), AuthError> {
        self.0.lock().unwrap().remove(key);
        Ok(())
    }
}

pub const DEV_TOKEN_KEY: &str = "gateway-dev-token";

/// The gateway's `COPILOT_DEV_TOKEN`, entered in the settings screen.
pub struct DevTokenProvider {
    store: Arc<dyn SecretStore>,
}

impl DevTokenProvider {
    pub fn new(store: Arc<dyn SecretStore>) -> Self {
        Self { store }
    }

    pub fn save(&self, token: &str) -> Result<(), AuthError> {
        if token.trim().is_empty() {
            self.store.delete(DEV_TOKEN_KEY)
        } else {
            self.store.set(DEV_TOKEN_KEY, token.trim())
        }
    }

    pub fn is_set(&self) -> bool {
        matches!(self.store.get(DEV_TOKEN_KEY), Ok(Some(_)))
    }

    fn read(&self) -> Result<Token, AuthError> {
        match self.store.get(DEV_TOKEN_KEY)? {
            Some(t) => Ok(Token { access_token: t, expires_at: None }),
            None => Err(AuthError::NotSignedIn("set the gateway dev token in Settings".into())),
        }
    }
}

impl TokenProvider for DevTokenProvider {
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move { self.read() })
    }

    /// The dev token never expires; refreshing re-reads it, which picks up a token changed in
    /// the settings during a call.
    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move { self.read() })
    }
}

/// A fixed list of tokens, one per refresh; for tests.
pub struct StaticTokens {
    tokens: Mutex<std::collections::VecDeque<Token>>,
    pub refreshes: std::sync::atomic::AtomicUsize,
}

impl StaticTokens {
    pub fn new(tokens: Vec<Token>) -> Self {
        Self { tokens: Mutex::new(tokens.into()), refreshes: Default::default() }
    }

    pub fn one(token: &str) -> Self {
        Self::new(vec![Token { access_token: token.into(), expires_at: None }])
    }
}

impl TokenProvider for StaticTokens {
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move {
            self.tokens
                .lock()
                .unwrap()
                .front()
                .cloned()
                .ok_or_else(|| AuthError::NotSignedIn("no token".into()))
        })
    }

    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move {
            self.refreshes.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let mut q = self.tokens.lock().unwrap();
            if q.len() > 1 {
                q.pop_front();
            }
            q.front().cloned().ok_or_else(|| AuthError::NotSignedIn("no token".into()))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn dev_token_round_trips_through_the_store() {
        let store = Arc::new(MemorySecretStore::default());
        let p = DevTokenProvider::new(store.clone());
        assert!(matches!(p.current().await, Err(AuthError::NotSignedIn(_))));
        p.save("  dev-token-0123456789  ").unwrap();
        assert_eq!(p.current().await.unwrap().access_token, "dev-token-0123456789");
        assert_eq!(p.refresh().await.unwrap().expires_at, None);
        p.save("").unwrap();
        assert!(!p.is_set());
    }
}
