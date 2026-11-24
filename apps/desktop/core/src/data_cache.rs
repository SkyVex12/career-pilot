//! One bounded repository shared by native commands and every webview. Private disk
//! copies are encrypted and always validated online after a process restart.

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{SystemTime, UNIX_EPOCH};

use aes_gcm::aead::{Aead, Payload};
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::api::{ApiError, ReadResponse, sha256_hex};
use crate::auth::SecretStore;

const MAX_BYTES: usize = 32 * 1024 * 1024;
const MAX_ENTRIES: usize = 256;
const MAX_DISK_ENTRY: usize = 2 * 1024 * 1024;
const KEY_NAME: &str = "data-cache-key-v1";

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

/// JWT claims are used only to partition storage, never as proof of authorization.
/// A refresh preserves the partition; opaque development tokens are hashed instead.
pub fn namespace(origin: &str, token: &str, profile: Option<&str>) -> String {
    let identity = token
        .split('.')
        .nth(1)
        .and_then(|s| base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s).ok())
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| {
            v.get("sub")
                .and_then(Value::as_str)
                .map(|sub| format!("{}:{sub}", v.get("iss").and_then(Value::as_str).unwrap_or("")))
        })
        .unwrap_or_else(|| sha256_hex(token.as_bytes()));
    sha256_hex(
        format!("v1\n{}\n{identity}\n{}", origin.trim().trim_end_matches('/'), profile.unwrap_or("me"))
            .as_bytes(),
    )
}

fn route(path: &str) -> &str {
    path.split('?').next().unwrap_or(path)
}

fn group(path: &str) -> &'static str {
    let p = route(path);
    if p == "/call-languages" {
        "catalog"
    } else if p.starts_with("/sessions") {
        "history"
    } else if p.starts_with("/employers") || p.starts_with("/proposals") || p.starts_with("/memory/") {
        "work"
    } else if p.starts_with("/ask/") {
        "ask"
    } else if p.starts_with("/profile/") {
        "preferences"
    } else {
        "career"
    }
}

fn history_target(path: &str) -> Option<&str> {
    if group(path) != "history" {
        return None;
    }
    route(path).trim_start_matches('/').split('/').nth(1).filter(|id| uuid::Uuid::parse_str(id).is_ok())
}

fn only_replay(path: &str) -> bool {
    group(path) == "work"
        && !(route(path).starts_with("/employers/") && route(path).matches('/').count() == 2)
}

fn lifetime(path: &str) -> Option<u64> {
    let p = route(path);
    let parts: Vec<_> = p.trim_start_matches('/').split('/').collect();
    match parts.as_slice() {
        ["call-languages"] => Some(86_400_000),
        ["profile", "call-preferences"] => Some(60_000),
        ["sessions"] => Some(30_000),
        ["sessions", id] if uuid::Uuid::parse_str(id).is_ok() => Some(30_000),
        ["sessions", id, "transcript" | "replay"] if uuid::Uuid::parse_str(id).is_ok() => Some(30_000),
        ["employers"] | ["proposals"] => Some(30_000),
        ["employers", _, "logs" | "documents" | "memory"] => Some(30_000),
        ["employers", _, "logs" | "documents", _] => Some(30_000),
        ["applications" | "interviews" | "resumes" | "documents" | "jobs"] => Some(15_000),
        ["applications" | "interviews" | "resumes" | "documents" | "jobs", id]
            if uuid::Uuid::parse_str(id).is_ok() =>
        {
            Some(15_000)
        }
        ["usage"] | ["ask", "conversations"] | ["ask", "context-options"] => Some(30_000),
        ["ask", "conversations", _] => Some(30_000),
        _ => None,
    }
}

fn pending(v: &Value) -> bool {
    match v {
        Value::Array(items) => items.iter().any(pending),
        Value::Object(obj) => {
            let status = obj.get("status").and_then(Value::as_str).unwrap_or("");
            let operation = obj.get("operation_state").and_then(Value::as_str).unwrap_or("");
            matches!(
                status,
                "live"
                    | "pending"
                    | "queued"
                    | "running"
                    | "processing"
                    | "extracting"
                    | "ingesting"
                    | "indexing"
                    | "ending"
            ) || matches!(operation, "queued" | "running")
                || obj.values().any(pending)
        }
        _ => false,
    }
}

fn ttl(path: &str, control: &str, value: &Value) -> Option<u64> {
    let mut result = lifetime(path)?;
    for directive in control.split(',').map(str::trim) {
        if directive.eq_ignore_ascii_case("no-store") {
            return None;
        }
        if directive.eq_ignore_ascii_case("no-cache") {
            result = 0;
        }
        if let Some((name, value)) = directive.split_once('=')
            && name.trim().eq_ignore_ascii_case("max-age")
            && let Ok(seconds) = value.trim_matches('"').parse::<u64>()
        {
            result = result.min(seconds.saturating_mul(1000));
        }
    }
    if pending(value) {
        result = 0;
    }
    Some(result)
}

#[derive(Clone, Serialize, Deserialize)]
struct Entry {
    path: String,
    value: Value,
    etag: Option<String>,
    account: Option<String>,
    workspace: Option<String>,
    checked: u64,
    ttl: u64,
    bytes: usize,
    #[serde(skip)]
    validated: bool,
    #[serde(skip)]
    used: u64,
}

#[derive(Default)]
struct State {
    entries: HashMap<String, Entry>,
    gates: HashMap<String, Weak<tokio::sync::Mutex<()>>>,
    scopes: HashMap<String, (String, String)>,
    cursors: HashMap<String, (String, u64)>,
    flights: HashMap<String, Weak<Flight>>,
}

#[derive(Default)]
struct Flight {
    result: tokio::sync::Mutex<Option<Result<Value, ApiError>>>,
    complete: AtomicBool,
    generation: AtomicU64,
}

struct Disk {
    dir: PathBuf,
    cipher: Aes256Gcm,
}

impl Disk {
    fn open(dir: PathBuf, secrets: &dyn SecretStore) -> Option<Self> {
        let encoded = match secrets.get(KEY_NAME).ok()? {
            Some(key) => key,
            None => {
                let key = base64::engine::general_purpose::STANDARD.encode(rand::random::<[u8; 32]>());
                secrets.set(KEY_NAME, &key).ok()?;
                key
            }
        };
        let key = base64::engine::general_purpose::STANDARD.decode(encoded).ok()?;
        let cipher = Aes256Gcm::new_from_slice(&key).ok()?;
        std::fs::create_dir_all(&dir).ok()?;
        Some(Self { dir, cipher })
    }

    fn path(&self, ns: &str, path: &str) -> PathBuf {
        let entity = route(path).trim_start_matches('/').split('/').nth(1).unwrap_or("collection");
        let entity = sha256_hex(entity.as_bytes());
        let representation = if route(path).ends_with("/replay") { "replay" } else { "body" };
        self.dir.join(format!(
            "{ns}-{}-{}-{representation}-{}.cache",
            group(path),
            &entity[..16],
            &sha256_hex(path.as_bytes())[..32]
        ))
    }

    fn read(&self, ns: &str, path: &str) -> Option<Entry> {
        let file = self.path(ns, path);
        if std::fs::metadata(&file).ok()?.len() > MAX_DISK_ENTRY as u64 + 64 {
            return None;
        }
        let bytes = std::fs::read(file).ok()?;
        if bytes.len() < 12 {
            return None;
        }
        let aad = format!("v1:{ns}:{path}");
        let plain = self
            .cipher
            .decrypt(Nonce::from_slice(&bytes[..12]), Payload { msg: &bytes[12..], aad: aad.as_bytes() })
            .ok()?;
        let mut entry: Entry = serde_json::from_slice(&plain).ok()?;
        if entry.path != path {
            return None;
        }
        // Only the non-sensitive language catalog may be served cold without a validation.
        entry.validated = path == "/call-languages";
        Some(entry)
    }

    fn write(&self, ns: &str, entry: &Entry) {
        let Ok(plain) = serde_json::to_vec(entry) else {
            return;
        };
        if plain.len() > MAX_DISK_ENTRY
            || entry.etag.is_none()
            || entry.account.is_none()
            || entry.workspace.is_none()
        {
            return;
        }
        let nonce = rand::random::<[u8; 12]>();
        let aad = format!("v1:{ns}:{}", entry.path);
        let Ok(encrypted) =
            self.cipher.encrypt(Nonce::from_slice(&nonce), Payload { msg: &plain, aad: aad.as_bytes() })
        else {
            return;
        };
        let mut bytes = nonce.to_vec();
        bytes.extend(encrypted);
        if std::fs::write(self.path(ns, &entry.path), bytes).is_err() {
            return;
        }
        let Ok(files) = std::fs::read_dir(&self.dir) else {
            return;
        };
        let mut files: Vec<_> = files
            .flatten()
            .filter_map(|f| {
                let meta = f.metadata().ok()?;
                (f.path().extension().is_some_and(|e| e == "cache") && meta.is_file())
                    .then(|| (meta.modified().ok(), meta.len(), f.path()))
            })
            .collect();
        files.sort_by_key(|f| f.0);
        let mut bytes: u64 = files.iter().map(|f| f.1).sum();
        let mut count = files.len();
        for (_, size, path) in files {
            if bytes <= MAX_BYTES as u64 && count <= MAX_ENTRIES {
                break;
            }
            let _ = std::fs::remove_file(path);
            bytes = bytes.saturating_sub(size);
            count -= 1;
        }
    }

    fn remove(&self, ns: &str, path: &str) {
        let _ = std::fs::remove_file(self.path(ns, path));
    }

    fn clear(&self, ns: Option<&str>, groups: &[&str]) {
        let Ok(files) = std::fs::read_dir(&self.dir) else {
            return;
        };
        for file in files.flatten() {
            let name = file.file_name().to_string_lossy().into_owned();
            if name.ends_with(".cache")
                && ns.is_none_or(|ns| name.starts_with(&format!("{ns}-")))
                && (groups.is_empty() || groups.iter().any(|g| name.contains(&format!("-{g}-"))))
            {
                let _ = std::fs::remove_file(file.path());
            }
        }
    }

    fn changes(&self, ns: &str, domain: &str, changes: &[Value]) {
        if changes.is_empty() {
            return;
        }
        if domain == "work" {
            self.clear(Some(ns), &[domain]);
            return;
        }
        let Ok(files) = std::fs::read_dir(&self.dir) else {
            return;
        };
        for file in files.flatten() {
            let name = file.file_name().to_string_lossy().into_owned();
            if !name.starts_with(&format!("{ns}-history-")) || !name.ends_with(".cache") {
                continue;
            }
            let affected = changes.iter().any(|c| {
                if c["resource"] == "references" {
                    return name.contains("-replay-");
                }
                let id = c["id"].as_str().unwrap_or("");
                name.contains(&format!("-{}-", &sha256_hex(id.as_bytes())[..16]))
            });
            if affected {
                let _ = std::fs::remove_file(file.path());
            }
        }
    }

    fn invalidate_history(&self, path: &str) {
        let Ok(files) = std::fs::read_dir(&self.dir) else {
            return;
        };
        let target = history_target(path).map(|id| sha256_hex(id.as_bytes()));
        let collection = sha256_hex(b"collection");
        for file in files.flatten() {
            let name = file.file_name().to_string_lossy().into_owned();
            if !name.ends_with(".cache") || !name.contains("-history-") {
                continue;
            }
            let affected = if only_replay(path) {
                name.contains("-replay-")
            } else if let Some(target) = &target {
                name.contains(&format!("-{}-", &target[..16]))
                    || name.contains(&format!("-{}-", &collection[..16]))
            } else {
                true
            };
            if affected {
                let _ = std::fs::remove_file(file.path());
            }
        }
    }
}

pub struct DataCache {
    state: Mutex<State>,
    epoch: AtomicU64,
    context: AtomicU64,
    disk: Option<Disk>,
}

impl Default for DataCache {
    fn default() -> Self {
        Self {
            state: Mutex::new(State::default()),
            epoch: AtomicU64::new(0),
            context: AtomicU64::new(0),
            disk: None,
        }
    }
}

impl DataCache {
    pub fn new(dir: PathBuf, secrets: &dyn SecretStore) -> Self {
        let disk = Disk::open(dir, secrets);
        if disk.is_none() {
            tracing::warn!("data cache persistence unavailable; using memory only");
        }
        Self { disk, ..Self::default() }
    }

    pub fn epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }
    pub fn context(&self) -> u64 {
        self.context.load(Ordering::SeqCst)
    }

    fn check(&self, epoch: u64) -> Result<(), ApiError> {
        if epoch != self.epoch() {
            Err(ApiError::Invalid("Data changed while loading; refresh this view.".into()))
        } else {
            Ok(())
        }
    }

    /// Fences pending requests as well as clearing sensitive data after identity changes.
    pub fn clear(&self) {
        let mut state = self.state.lock().unwrap();
        self.epoch.fetch_add(1, Ordering::SeqCst);
        self.context.fetch_add(1, Ordering::SeqCst);
        state.entries.clear();
        state.cursors.clear();
        state.scopes.clear();
        if let Some(disk) = &self.disk {
            disk.clear(None, &[]);
        }
    }

    /// Call before AND after a write: an older GET cannot repopulate the cache during it.
    pub fn invalidate(&self, path: &str) {
        let p = route(path);
        if p == "/me" || p.starts_with("/profiles") || p.starts_with("/career-os") || p.starts_with("/auth") {
            self.clear();
            return;
        }
        let groups: &[&str] = match group(path) {
            "work" => &["work", "history", "ask", "career"],
            "history" => &["history", "work", "career", "ask"],
            "preferences" => &["preferences"],
            "ask" => &["ask"],
            _ => &["career", "ask"],
        };
        let mut state = self.state.lock().unwrap();
        self.epoch.fetch_add(1, Ordering::SeqCst);
        state.entries.retain(|_, e| {
            if group(&e.path) == "history" && groups.contains(&"history") {
                if only_replay(path) {
                    return !route(&e.path).ends_with("/replay");
                }
                if let Some(id) = history_target(path) {
                    return route(&e.path) != "/sessions" && history_target(&e.path) != Some(id);
                }
            }
            !groups.contains(&group(&e.path))
        });
        // Keep the committed sync watermark: our own writes will appear in its next
        // delta just like another device's writes. Resetting it would discard unrelated history.
        if let Some(disk) = &self.disk {
            let ordinary: Vec<_> = groups.iter().copied().filter(|g| *g != "history").collect();
            if !ordinary.is_empty() {
                disk.clear(None, &ordinary);
            }
            if groups.contains(&"history") {
                disk.invalidate_history(path);
            }
        }
    }

    fn gate(&self, key: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut state = self.state.lock().unwrap();
        state.gates.retain(|_, v| v.strong_count() > 0);
        if let Some(gate) = state.gates.get(key).and_then(Weak::upgrade) {
            return gate;
        }
        let gate = Arc::new(tokio::sync::Mutex::new(()));
        state.gates.insert(key.into(), Arc::downgrade(&gate));
        gate
    }

    fn entry(&self, ns: &str, path: &str) -> Option<Entry> {
        let key = format!("{ns}:{path}");
        let mut state = self.state.lock().unwrap();
        if let Some(entry) = state.entries.get_mut(&key) {
            entry.used = now();
            return Some(entry.clone());
        }
        self.disk.as_ref().and_then(|d| d.read(ns, path))
    }

    fn keep(&self, ns: &str, mut entry: Entry, epoch: u64) -> Result<(), ApiError> {
        let mut state = self.state.lock().unwrap();
        self.check(epoch)?;
        entry.used = now();
        if entry.bytes > MAX_BYTES {
            return Ok(());
        }
        if let Some(disk) = &self.disk {
            disk.write(ns, &entry);
        }
        state.entries.insert(format!("{ns}:{}", entry.path), entry);
        while state.entries.len() > MAX_ENTRIES
            || state.entries.values().map(|e| e.bytes).sum::<usize>() > MAX_BYTES
        {
            let Some(key) = state.entries.iter().min_by_key(|(_, e)| e.used).map(|(k, _)| k.clone()) else {
                break;
            };
            state.entries.remove(&key);
        }
        Ok(())
    }

    fn scope(&self, ns: &str, read: &ReadResponse, epoch: u64) -> Result<(), ApiError> {
        let mut state = self.state.lock().unwrap();
        self.check(epoch)?;
        if let (Some(account), Some(workspace)) = (&read.account, &read.workspace) {
            let pair = (account.clone(), workspace.clone());
            if state.scopes.get(ns).is_some_and(|previous| previous != &pair) {
                state.entries.retain(|key, _| !key.starts_with(&format!("{ns}:")));
                state.cursors.retain(|key, _| !key.starts_with(&format!("{ns}:")));
                if let Some(disk) = &self.disk {
                    disk.clear(Some(ns), &[]);
                }
                state.scopes.insert(ns.into(), pair);
                self.epoch.fetch_add(1, Ordering::SeqCst);
                self.context.fetch_add(1, Ordering::SeqCst);
                return Err(ApiError::Invalid("Workspace access changed; refresh this view.".into()));
            }
            state.scopes.insert(ns.into(), pair);
        }
        Ok(())
    }

    /// Sync is a demand-driven invalidation feed, never an always-on polling connection.
    async fn sync<F, Fut>(&self, ns: &str, domain: &str, epoch: u64, fetch: &F) -> Result<(), ApiError>
    where
        F: Fn(String, Option<String>) -> Fut,
        Fut: Future<Output = Result<ReadResponse, ApiError>>,
    {
        let key = format!("{ns}:{domain}");
        let gate = self.gate(&format!("sync:{key}"));
        let _one = gate.lock().await;
        self.check(epoch)?;
        let mut cursor = self.state.lock().unwrap().cursors.get(&key).cloned();
        let bootstrap = cursor.is_none();
        if cursor.as_ref().is_some_and(|(_, checked)| now().saturating_sub(*checked) < 30_000) {
            return Ok(());
        }
        // Bound a foreground catch-up. A large backlog is safely reset on the next read.
        for _ in 0..20 {
            let path = match &cursor {
                Some((c, _)) => format!("/sync?domain={domain}&cursor={c}"),
                None => format!("/sync?domain={domain}"),
            };
            let read = match fetch(path, None).await {
                Ok(read) => read,
                Err(ApiError::Status { status: 404 | 405, .. }) => {
                    // An older API is checked at most once per freshness window.
                    self.state.lock().unwrap().cursors.insert(key, (String::new(), now()));
                    return Ok(());
                }
                Err(error) => return Err(error),
            };
            self.scope(ns, &read, epoch)?;
            let Some(value) = read.value else {
                return Err(ApiError::Invalid("empty sync response".into()));
            };
            let reset = value["reset"].as_bool().unwrap_or(true);
            let changes = value["changes"].as_array().cloned().unwrap_or_default();
            let next = value["cursor"]
                .as_str()
                .ok_or_else(|| ApiError::Invalid("missing sync cursor".into()))?
                .to_string();
            let more = value["has_more"].as_bool().unwrap_or(false);
            {
                let mut state = self.state.lock().unwrap();
                self.check(epoch)?;
                state.entries.retain(|k, entry| {
                    if !k.starts_with(&format!("{ns}:")) || group(&entry.path) != domain {
                        return true;
                    }
                    let affected = reset
                        || changes.iter().any(|change| {
                            let id = change["id"].as_str().unwrap_or("");
                            // Lists and work aggregates depend on child records. Session bodies
                            // are individually invalidated; reference labels only affect replay.
                            if domain == "work" {
                                return true;
                            }
                            if change["resource"] == "references" {
                                return entry.path.contains("/replay");
                            }
                            route(&entry.path) == "/sessions" || entry.path.contains(id)
                        });
                    if affected {
                        if let Some(disk) = &self.disk {
                            disk.remove(ns, &entry.path);
                        }
                    }
                    !affected
                });
                // Private disk bodies always receive per-resource authorization on cold
                // reads. A first bootstrap can retain them as conditional candidates.
                if let Some(disk) = &self.disk {
                    if reset && !bootstrap {
                        disk.clear(Some(ns), &[domain]);
                    } else {
                        disk.changes(ns, domain, &changes);
                    }
                }
                state.cursors.insert(key.clone(), (next.clone(), if more { 0 } else { now() }));
            }
            cursor = Some((next, 0));
            if !more {
                return Ok(());
            }
        }
        // No stale response is served when the bounded catch-up was not enough.
        Err(ApiError::Invalid("Many changes are syncing; refresh to continue.".into()))
    }

    pub async fn read<F, Fut>(
        &self,
        ns: &str,
        path: &str,
        force: bool,
        context: u64,
        fetch: F,
    ) -> Result<Value, ApiError>
    where
        F: Fn(String, Option<String>) -> Fut,
        Fut: Future<Output = Result<ReadResponse, ApiError>>,
    {
        let flight = {
            let mut state = self.state.lock().unwrap();
            if context != self.context() {
                return Err(ApiError::Invalid("Workspace changed while loading.".into()));
            }
            let key = format!("{ns}:{path}:{}", self.epoch());
            state.flights.retain(|_, f| f.strong_count() > 0);
            if let Some(flight) =
                state.flights.get(&key).and_then(Weak::upgrade).filter(|f| !f.complete.load(Ordering::SeqCst))
            {
                flight
            } else {
                let flight = Arc::new(Flight::default());
                state.flights.insert(key, Arc::downgrade(&flight));
                flight
            }
        };
        let mut result = flight.result.lock().await;
        if context != self.context() {
            return Err(ApiError::Invalid("Workspace changed while loading.".into()));
        }
        if let Some(value) = result.as_ref()
            && self.epoch() == flight.generation.load(Ordering::SeqCst)
        {
            return value.clone();
        }
        let mut generation = self.epoch();
        let mut value = self.read_one(ns, path, force, generation, &fetch).await;
        // A write to another resource may overlap this read. Retry against the new
        // generation, but never across an identity, deployment or workspace transition.
        for _ in 0..2 {
            if context != self.context() || generation == self.epoch() {
                break;
            }
            generation = self.epoch();
            value = self.read_one(ns, path, force, generation, &fetch).await;
        }
        if context != self.context() {
            value = Err(ApiError::Invalid("Workspace changed while loading.".into()));
        } else if generation != self.epoch() {
            value = self.check(generation).map(|_| Value::Null);
        }
        *result = Some(value.clone());
        flight.generation.store(generation, Ordering::SeqCst);
        flight.complete.store(true, Ordering::SeqCst);
        value
    }

    async fn read_one<F, Fut>(
        &self,
        ns: &str,
        path: &str,
        force: bool,
        epoch: u64,
        fetch: &F,
    ) -> Result<Value, ApiError>
    where
        F: Fn(String, Option<String>) -> Fut,
        Fut: Future<Output = Result<ReadResponse, ApiError>>,
    {
        crate::api::check_path(path)?;
        self.check(epoch)?;
        let policy = lifetime(path);
        let entry = policy.and_then(|_| self.entry(ns, path));
        if let Some(e) = &entry
            && e.validated
            && now().saturating_sub(e.checked) < e.ttl
            && !force
        {
            tracing::debug!(resource = route(path), "data cache hit");
            self.check(epoch)?;
            return Ok(e.value.clone());
        }
        let domain = group(path);
        if policy.is_some() && matches!(domain, "history" | "work") {
            self.sync(ns, domain, epoch, fetch).await?;
            // A reset removes stored candidates, but this captured body may still be
            // used after an authoritative per-resource 304. A deleted resource instead
            // returns 404. This preserves cold-disk revalidation without serving it stale.
        }
        let tag = entry.as_ref().and_then(|e| e.etag.clone());
        let read = match fetch(path.into(), tag).await {
            Ok(read) => read,
            Err(error) => {
                if matches!(error.status(), Some(401 | 403 | 404)) {
                    let mut state = self.state.lock().unwrap();
                    state.entries.remove(&format!("{ns}:{path}"));
                    if let Some(disk) = &self.disk {
                        disk.remove(ns, path);
                    }
                }
                return Err(error);
            }
        };
        self.scope(ns, &read, epoch)?;
        let value = match read.value {
            Some(value) => value,
            None => {
                let e = entry
                    .as_ref()
                    .ok_or_else(|| ApiError::Invalid("304 without a cached response".into()))?;
                if e.account != read.account || e.workspace != read.workspace {
                    return Err(ApiError::Invalid("304 scope mismatch".into()));
                }
                e.value.clone()
            }
        };
        if let Some(ttl) = ttl(path, &read.cache_control, &value) {
            let bytes = serde_json::to_vec(&value).map(|b| b.len()).unwrap_or(MAX_BYTES + 1);
            self.keep(
                ns,
                Entry {
                    path: path.into(),
                    value: value.clone(),
                    etag: read.etag,
                    account: read.account,
                    workspace: read.workspace,
                    checked: now(),
                    ttl,
                    bytes,
                    validated: true,
                    used: now(),
                },
                epoch,
            )?;
        } else {
            let mut state = self.state.lock().unwrap();
            self.check(epoch)?;
            state.entries.remove(&format!("{ns}:{path}"));
            if let Some(disk) = &self.disk {
                disk.remove(ns, path);
            }
        }
        tracing::debug!(resource = route(path), "data cache validated");
        self.check(epoch)?;
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::MemorySecretStore;
    use serde_json::json;

    fn response(value: Value) -> ReadResponse {
        ReadResponse {
            value: Some(value),
            etag: Some("\"v1\"".into()),
            account: Some("a".into()),
            workspace: Some("w".into()),
            cache_control: "private, max-age=30".into(),
        }
    }

    #[tokio::test]
    async fn shares_reads_and_forced_validation_reuses_304() {
        let cache = DataCache::default();
        let calls = AtomicU64::new(0);
        let fetch = |_: String, tag: Option<String>| {
            calls.fetch_add(1, Ordering::SeqCst);
            async move {
                tokio::task::yield_now().await;
                let mut r = response(json!([1]));
                if tag.is_some() {
                    r.value = None;
                }
                Ok(r)
            }
        };
        let (a, b) = tokio::join!(
            cache.read("a", "/applications", false, 0, &fetch),
            cache.read("a", "/applications", false, 0, &fetch)
        );
        assert_eq!(a.unwrap(), b.unwrap());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        cache.read("a", "/applications", true, 0, &fetch).await.unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn changes_fence_old_responses_and_live_reads_never_hit() {
        let cache = DataCache::default();
        let result = cache
            .read("a", "/applications", false, 0, |_, _| async {
                cache.invalidate("/applications/1");
                Ok(response(json!(["old"])))
            })
            .await;
        assert!(result.is_err());
        for path in [
            "/sessions/live",
            "/seats",
            "/profiles",
            "/applications/status",
            "/sessions/123/status",
            "/ask/conversations/1/messages/2",
            "/practice/sessions/1",
        ] {
            assert_eq!(lifetime(path), None, "{path}");
        }
        assert_eq!(ttl("/applications", "no-store", &json!([])), None);
        assert_eq!(ttl("/applications", "max-age=0", &json!([])), Some(0));
        assert_eq!(ttl("/applications", "", &json!({"pack":{"operation_state":"running"}})), Some(0));
    }

    #[tokio::test]
    async fn encrypted_private_disk_requires_online_validation_and_clear_removes_it() {
        let dir = tempfile::tempdir().unwrap();
        let secrets = MemorySecretStore::default();
        let cache = DataCache::new(dir.path().into(), &secrets);
        cache
            .read("a", "/applications", false, 0, |_, _| async {
                Ok(response(json!({"secret":"private text"})))
            })
            .await
            .unwrap();
        let file = std::fs::read_dir(dir.path()).unwrap().next().unwrap().unwrap().path();
        assert!(!String::from_utf8_lossy(&std::fs::read(file).unwrap()).contains("private text"));
        let cold = DataCache::new(dir.path().into(), &secrets);
        let value = cold
            .read("a", "/applications", false, 0, |_, tag| async move {
                assert_eq!(tag.as_deref(), Some("\"v1\""));
                let mut r = response(Value::Null);
                r.value = None;
                Ok(r)
            })
            .await
            .unwrap();
        assert_eq!(value["secret"], "private text");
        cold.clear();
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn concurrent_no_store_reads_share_only_the_active_flight() {
        let cache = DataCache::default();
        let calls = AtomicU64::new(0);
        let fetch = |_: String, _: Option<String>| {
            calls.fetch_add(1, Ordering::SeqCst);
            async {
                tokio::task::yield_now().await;
                let mut r = response(json!({"pending": true}));
                r.cache_control = "no-store".into();
                Ok(r)
            }
        };
        let (a, b) = tokio::join!(
            cache.read("a", "/applications/status", true, 0, &fetch),
            cache.read("a", "/applications/status", true, 0, &fetch)
        );
        assert_eq!(a.unwrap(), b.unwrap());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        cache.read("a", "/applications/status", true, 0, &fetch).await.unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(cache.state.lock().unwrap().entries.is_empty());
    }

    #[tokio::test]
    async fn mutation_overlap_retries_but_identity_change_does_not() {
        let cache = DataCache::default();
        let calls = AtomicU64::new(0);
        let value = cache
            .read("a", "/applications", false, 0, |_, _| {
                let n = calls.fetch_add(1, Ordering::SeqCst);
                if n == 0 {
                    cache.invalidate("/profile/call-preferences");
                }
                async move { Ok(response(json!(n))) }
            })
            .await
            .unwrap();
        assert_eq!(value, json!(1));
        let calls = AtomicU64::new(0);
        let value = cache
            .read("a", "/applications", true, 0, |_, _| {
                calls.fetch_add(1, Ordering::SeqCst);
                cache.clear();
                async { Ok(response(json!("old account"))) }
            })
            .await;
        assert!(value.is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(cache.state.lock().unwrap().entries.is_empty());
    }

    #[tokio::test]
    async fn sync_reset_still_allows_authoritative_cold_resource_validation() {
        let dir = tempfile::tempdir().unwrap();
        let secrets = MemorySecretStore::default();
        let cache = DataCache::new(dir.path().into(), &secrets);
        let path = "/sessions/00000000-0000-0000-0000-000000000001/transcript";
        let fetch = |p: String, _: Option<String>| async move {
            if p.starts_with("/sync?") {
                Ok(response(json!({"reset":true,"changes":[],"has_more":false,"cursor":"first"})))
            } else {
                Ok(response(json!({"text":"private transcript"})))
            }
        };
        cache.read("a", path, false, 0, fetch).await.unwrap();
        let cold = DataCache::new(dir.path().into(), &secrets);
        let value = cold
            .read("a", path, false, 0, |p, tag| async move {
                if p.starts_with("/sync?") {
                    Ok(response(json!({"reset":true,"changes":[],"has_more":false,"cursor":"second"})))
                } else {
                    assert!(tag.is_some());
                    let mut r = response(Value::Null);
                    r.value = None;
                    Ok(r)
                }
            })
            .await
            .unwrap();
        assert_eq!(value["text"], "private transcript");
    }

    #[tokio::test]
    async fn forbidden_or_changed_scope_never_falls_back_to_disk() {
        let dir = tempfile::tempdir().unwrap();
        let cache = DataCache::new(dir.path().into(), &MemorySecretStore::default());
        cache.read("a", "/applications", false, 0, |_, _| async { Ok(response(json!([1]))) }).await.unwrap();
        let denied = cache
            .read("a", "/applications", true, 0, |_, _| async {
                Err(ApiError::Status { status: 403, detail: "revoked".into() })
            })
            .await;
        assert_eq!(denied.unwrap_err().status(), Some(403));
        assert!(cache.entry("a", "/applications").is_none());
        let changed = cache
            .read("a", "/applications", true, 0, |_, _| async {
                let mut r = response(json!([2]));
                r.workspace = Some("new".into());
                Ok(r)
            })
            .await;
        assert!(changed.is_err());
        assert_eq!(cache.context(), 1);
    }

    #[tokio::test]
    async fn one_session_tombstone_preserves_other_persisted_transcripts() {
        let dir = tempfile::tempdir().unwrap();
        let secrets = MemorySecretStore::default();
        let cache = DataCache::new(dir.path().into(), &secrets);
        let first = "/sessions/00000000-0000-0000-0000-000000000001/transcript";
        let second = "/sessions/00000000-0000-0000-0000-000000000002/transcript";
        let fetch = |p: String, _: Option<String>| async move {
            if p.starts_with("/sync?") {
                Ok(response(json!({"reset":true,"changes":[],"has_more":false,"cursor":"first"})))
            } else {
                Ok(response(json!({"text":p})))
            }
        };
        cache.read("a", first, false, 0, &fetch).await.unwrap();
        cache.read("a", second, false, 0, &fetch).await.unwrap();
        let cold = DataCache::new(dir.path().into(), &secrets);
        cold.read("a", first, false, 0, &fetch).await.unwrap();
        assert!(cold.entry("a", second).is_some(), "bootstrap must retain other online-only candidates");
        cold.state.lock().unwrap().cursors.get_mut("a:history").unwrap().1 = 0;
        let deleted = cold.read("a", first, true, 0, |p, _| async move {
            if p.starts_with("/sync?") { Ok(response(json!({"reset":false,"changes":[{"resource":"sessions","id":"00000000-0000-0000-0000-000000000001","deleted":true}],"has_more":false,"cursor":"next"}))) }
            else { Err(ApiError::Status { status:404, detail:"deleted".into() }) }
        }).await;
        assert_eq!(deleted.unwrap_err().status(), Some(404));
        assert!(cold.entry("a", first).is_none());
        assert!(cold.entry("a", second).is_some());
    }

    #[tokio::test]
    async fn disk_tampering_and_no_store_never_produce_a_cache_hit() {
        let dir = tempfile::tempdir().unwrap();
        let secrets = MemorySecretStore::default();
        let cache = DataCache::new(dir.path().into(), &secrets);
        cache.read("a", "/applications", false, 0, |_, _| async { Ok(response(json!([1]))) }).await.unwrap();
        let path = cache.disk.as_ref().unwrap().path("a", "/applications");
        let mut bytes = std::fs::read(&path).unwrap();
        *bytes.last_mut().unwrap() ^= 1;
        std::fs::write(path, bytes).unwrap();
        let cold = DataCache::new(dir.path().into(), &secrets);
        cold.read("a", "/applications", false, 0, |_, tag| async move {
            assert!(tag.is_none());
            let mut r = response(json!([2]));
            r.cache_control = "no-store".into();
            Ok(r)
        })
        .await
        .unwrap();
        assert!(cold.entry("a", "/applications").is_none());
    }

    #[tokio::test]
    async fn local_note_and_fact_edits_preserve_unrelated_transcript_bodies() {
        let dir = tempfile::tempdir().unwrap();
        let cache = DataCache::new(dir.path().into(), &MemorySecretStore::default());
        let first = "/sessions/00000000-0000-0000-0000-000000000001/transcript";
        let second = "/sessions/00000000-0000-0000-0000-000000000002/transcript";
        let fetch = |p: String, _: Option<String>| async move {
            if p.starts_with("/sync?") {
                Ok(response(json!({"reset":true,"changes":[],"has_more":false,"cursor":"first"})))
            } else {
                Ok(response(json!({"text":p})))
            }
        };
        cache.read("a", first, false, 0, &fetch).await.unwrap();
        cache.read("a", second, false, 0, &fetch).await.unwrap();
        cache.invalidate("/sessions/00000000-0000-0000-0000-000000000001/notes");
        assert!(cache.entry("a", first).is_none());
        assert!(cache.entry("a", second).is_some());
        cache.invalidate("/memory/00000000-0000-0000-0000-000000000099");
        assert!(cache.entry("a", second).is_some());
        assert!(cache.state.lock().unwrap().cursors.contains_key("a:history"));
    }

    #[test]
    fn namespaces_follow_identity_not_rotating_token_or_vm_ip() {
        let token = |extra: &str| {
            format!(
                "x.{}.sig",
                base64::engine::general_purpose::URL_SAFE_NO_PAD
                    .encode(format!("{{\"sub\":\"same\",\"iss\":\"issuer\",\"extra\":\"{extra}\"}}"))
            )
        };
        assert_eq!(
            namespace("https://{host}/v1", &token("1"), None),
            namespace("https://{host}/v1", &token("2"), None)
        );
        assert_ne!(
            namespace("https://{host}/v1", &token("1"), None),
            namespace("https://{host}/v1", &token("1"), Some("other"))
        );
    }
}
