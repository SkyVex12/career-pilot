//! The user's own audio (ARCHITECTURE.md, "Desktop client", "Session lifecycle and resume").
//!
//! Live, the mic channel sends only speech timing. The echo-cancelled mic audio is recorded
//! on disk, encrypted, and after the socket closes it is uploaded through
//! `POST /sessions/{id}/me-audio` (a presigned S3 URL) and `POST .../me-audio/complete`; the
//! local copy is deleted once the upload succeeds. A failed upload stays on disk and is tried
//! again on the next app start. Without transcript consent nothing is recorded at all
//! ([`NoRecording`]).
//!
//! **At rest:** AES-256-GCM under a per-install key kept in the OS credential store (Windows
//! Credential Manager), never in a file. A recording is a header (`CPMA`, version, a random
//! 16-byte file id) and chunks of one second of 16 kHz pcm16: `u32` big-endian ciphertext
//! length, a random 12-byte nonce, the ciphertext with its tag. Each chunk's associated data
//! is the file id and the chunk's index, so chunks cannot be reordered, dropped from the
//! middle, or moved between files. A crash loses at most the second being written; a torn
//! last chunk is ignored on read.
//!
//! **Sessions:** the recording belongs to the session the client goes live with; if the
//! client has to start a new session mid-call (a resume closed with `4408`), the recording
//! is cut there and the rest belongs to the new session. A recording that never went live is
//! deleted.
//!
//! **Upload:** the file is decrypted chunk by chunk into a streamed WAV body, so the
//! plaintext never touches the disk and a long call is not held in memory.

use std::fs::{self, File, OpenOptions};
use std::future::Future;
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::api::{ApiClient, ApiError, PresignedUpload};
use crate::auth::{AuthError, SecretStore};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;
pub type SessionBinder = Arc<dyn Fn(Uuid) + Send + Sync>;

pub trait MeAudioSink: Send {
    /// One tick of the user's echo-cancelled microphone audio, 16 kHz mono.
    fn on_audio(&mut self, capture_ms: u32, samples: &[f32]);
    /// The capture stopped: write out what is buffered.
    fn finish(&mut self);
    /// Called with every session id the client goes live with, so the recording knows whose
    /// it is. None for sinks that keep nothing.
    fn session_binder(&self) -> Option<SessionBinder> {
        None
    }
}

/// No transcript consent: nothing is recorded, so nothing is uploaded.
#[derive(Default)]
pub struct NoRecording;

impl MeAudioSink for NoRecording {
    fn on_audio(&mut self, _capture_ms: u32, _samples: &[f32]) {}
    fn finish(&mut self) {}
}

pub const RECORDING_KEY: &str = "me-audio-key";
const MAGIC: &[u8; 4] = b"CPMA";
const VERSION: u8 = 1;
const HEADER_LEN: usize = 4 + 1 + 16;
pub const SAMPLE_RATE: u32 = 16_000;
/// One second of pcm16.
const CHUNK_BYTES: usize = SAMPLE_RATE as usize * 2;
const MAX_CIPHERTEXT: usize = CHUNK_BYTES + 16;
const AUDIO_EXT: &str = "cpma";
const MANIFEST_EXT: &str = "json";

#[derive(Debug, thiserror::Error)]
pub enum RecordingError {
    #[error("recording file: {0}")]
    Io(#[from] io::Error),
    #[error("the recording is not readable with this install's key, or was altered")]
    Corrupt,
    #[error("{0}")]
    Key(#[from] AuthError),
}

/// The per-install key, 32 random bytes, hex in the credential store.
#[derive(Clone)]
pub struct RecordingKey([u8; 32]);

impl RecordingKey {
    pub fn load(store: &dyn SecretStore) -> Result<Option<Self>, AuthError> {
        let Some(hex) = store.get(RECORDING_KEY)? else { return Ok(None) };
        let bytes = decode_hex(&hex).filter(|b| b.len() == 32);
        let bytes = bytes.ok_or_else(|| AuthError::Store("the recording key is malformed".into()))?;
        Ok(Some(Self(bytes.try_into().expect("32 bytes"))))
    }

    pub fn load_or_create(store: &dyn SecretStore) -> Result<Self, AuthError> {
        if let Some(key) = Self::load(store)? {
            return Ok(key);
        }
        let mut bytes = [0u8; 32];
        rand::rng().fill_bytes(&mut bytes);
        store.set(RECORDING_KEY, &encode_hex(&bytes))?;
        Ok(Self(bytes))
    }

    fn cipher(&self) -> Aes256Gcm {
        Aes256Gcm::new_from_slice(&self.0).expect("32-byte key")
    }
}

fn encode_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn decode_hex(s: &str) -> Option<Vec<u8>> {
    let s = s.trim();
    if !s.len().is_multiple_of(2) {
        return None;
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(s.get(i..i + 2)?, 16).ok()).collect()
}

fn chunk_aad(file_id: &[u8; 16], index: u64) -> [u8; 24] {
    let mut aad = [0u8; 24];
    aad[..16].copy_from_slice(file_id);
    aad[16..].copy_from_slice(&index.to_be_bytes());
    aad
}

/// `{id}.json` next to `{id}.cpma`: whose recording it is and whether it was closed.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Manifest {
    pub session_id: Option<Uuid>,
    pub started_at: u64,
    pub finished: bool,
}

impl Manifest {
    fn save(&self, path: &Path) -> io::Result<()> {
        let tmp = path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_vec(self)?)?;
        fs::rename(tmp, path)
    }
}

fn now_s() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// One recording file being written.
struct Writer {
    file: BufWriter<File>,
    audio_path: PathBuf,
    manifest_path: PathBuf,
    manifest: Manifest,
    file_id: [u8; 16],
    index: u64,
    cipher: Aes256Gcm,
    buf: Vec<u8>,
}

impl Writer {
    fn create(dir: &Path, key: &RecordingKey, session_id: Option<Uuid>) -> io::Result<Self> {
        let mut file_id = [0u8; 16];
        rand::rng().fill_bytes(&mut file_id);
        let name = encode_hex(&file_id);
        let audio_path = dir.join(format!("{name}.{AUDIO_EXT}"));
        let manifest_path = dir.join(format!("{name}.{MANIFEST_EXT}"));
        let manifest = Manifest { session_id, started_at: now_s(), finished: false };
        manifest.save(&manifest_path)?;
        let mut file = BufWriter::new(OpenOptions::new().write(true).create_new(true).open(&audio_path)?);
        file.write_all(MAGIC)?;
        file.write_all(&[VERSION])?;
        file.write_all(&file_id)?;
        file.flush()?;
        Ok(Self {
            file,
            audio_path,
            manifest_path,
            manifest,
            file_id,
            index: 0,
            cipher: key.cipher(),
            buf: Vec::with_capacity(CHUNK_BYTES),
        })
    }

    fn push(&mut self, samples: &[f32]) -> io::Result<()> {
        for s in samples {
            let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
            self.buf.extend_from_slice(&v.to_le_bytes());
            if self.buf.len() >= CHUNK_BYTES {
                self.write_chunk()?;
            }
        }
        Ok(())
    }

    fn write_chunk(&mut self) -> io::Result<()> {
        if self.buf.is_empty() {
            return Ok(());
        }
        let mut nonce = [0u8; 12];
        rand::rng().fill_bytes(&mut nonce);
        let aad = chunk_aad(&self.file_id, self.index);
        let ct = self
            .cipher
            .encrypt(Nonce::from_slice(&nonce), Payload { msg: &self.buf, aad: &aad })
            .map_err(|_| io::Error::other("encryption failed"))?;
        self.file.write_all(&(ct.len() as u32).to_be_bytes())?;
        self.file.write_all(&nonce)?;
        self.file.write_all(&ct)?;
        self.file.flush()?; // a crash loses at most this second
        self.index += 1;
        self.buf.clear();
        Ok(())
    }

    fn bind(&mut self, sid: Uuid) -> io::Result<()> {
        self.manifest.session_id = Some(sid);
        self.manifest.save(&self.manifest_path)
    }

    /// Close the file; one that never belonged to a session is deleted.
    fn close(mut self) -> io::Result<Option<PathBuf>> {
        self.write_chunk()?;
        self.file.flush()?;
        drop(self.file);
        if self.manifest.session_id.is_none() {
            let _ = fs::remove_file(&self.audio_path);
            let _ = fs::remove_file(&self.manifest_path);
            return Ok(None);
        }
        self.manifest.finished = true;
        self.manifest.save(&self.manifest_path)?;
        Ok(Some(self.audio_path))
    }
}

struct RecorderState {
    dir: PathBuf,
    key: RecordingKey,
    current: Option<Writer>,
}

impl RecorderState {
    fn fail(&mut self, e: io::Error) {
        tracing::warn!("recording stopped: {e}");
        if let Some(w) = self.current.take() {
            let _ = w.close();
        }
    }

    fn bind(&mut self, sid: Uuid) {
        let bound = self.current.as_ref().map(|w| w.manifest.session_id);
        let result = match bound {
            Some(None) => self.current.as_mut().unwrap().bind(sid),
            Some(Some(old)) if old != sid => {
                // A new session mid-call: the rest of the audio is the new session's.
                let old = self.current.take().unwrap();
                old.close().and_then(|_| {
                    self.current = Some(Writer::create(&self.dir, &self.key, Some(sid))?);
                    Ok(())
                })
            }
            _ => Ok(()),
        };
        if let Err(e) = result {
            self.fail(e);
        }
    }
}

/// Records the mic channel for the sessions of one engine run. Clone it to keep a handle
/// after the pipeline takes the sink.
#[derive(Clone)]
pub struct Recorder {
    state: Arc<Mutex<RecorderState>>,
}

impl Recorder {
    pub fn start(dir: &Path, key: RecordingKey) -> io::Result<Self> {
        fs::create_dir_all(dir)?;
        let writer = Writer::create(dir, &key, None)?;
        Ok(Self {
            state: Arc::new(Mutex::new(RecorderState { dir: dir.into(), key, current: Some(writer) })),
        })
    }

    pub fn bind(&self, sid: Uuid) {
        self.state.lock().unwrap().bind(sid);
    }
}

impl MeAudioSink for Recorder {
    fn on_audio(&mut self, _capture_ms: u32, samples: &[f32]) {
        let mut state = self.state.lock().unwrap();
        if let Some(w) = state.current.as_mut()
            && let Err(e) = w.push(samples)
        {
            state.fail(e);
        }
    }

    fn finish(&mut self) {
        let mut state = self.state.lock().unwrap();
        if let Some(w) = state.current.take()
            && let Err(e) = w.close()
        {
            tracing::warn!("closing the recording: {e}");
        }
    }

    fn session_binder(&self) -> Option<SessionBinder> {
        let me = self.clone();
        Some(Arc::new(move |sid| me.bind(sid)))
    }
}

/// The user's-audio sink for a new session: an encrypted recording in `dir` with transcript
/// consent (creating this install's key on first use), and nothing at all without it.
pub fn sink_for_session(
    store_transcript: bool,
    dir: &Path,
    secrets: &dyn SecretStore,
) -> Result<Box<dyn MeAudioSink>, String> {
    if !store_transcript {
        return Ok(Box::new(NoRecording));
    }
    let key = RecordingKey::load_or_create(secrets).map_err(|e| e.to_string())?;
    Ok(Box::new(Recorder::start(dir, key).map_err(|e| format!("recording folder {}: {e}", dir.display()))?))
}

/// Read a recording's chunks in order and hand each one's plaintext to `f`, stopping after
/// `limit` plaintext bytes. Returns the plaintext length read. A torn last chunk ends the
/// read; anything that fails authentication is [`RecordingError::Corrupt`].
fn read_chunks(
    path: &Path,
    key: &RecordingKey,
    limit: u64,
    mut f: impl FnMut(&[u8]) -> io::Result<()>,
) -> Result<u64, RecordingError> {
    let mut file = io::BufReader::new(File::open(path)?);
    let mut header = [0u8; HEADER_LEN];
    if file.read_exact(&mut header).is_err() || &header[..4] != MAGIC || header[4] != VERSION {
        return Err(RecordingError::Corrupt);
    }
    let file_id: [u8; 16] = header[5..].try_into().unwrap();
    let cipher = key.cipher();
    let mut total = 0u64;
    let mut index = 0u64;
    let mut ct = Vec::with_capacity(MAX_CIPHERTEXT);
    while total < limit {
        let mut len = [0u8; 4];
        let mut nonce = [0u8; 12];
        if !read_full(&mut file, &mut len)? || !read_full(&mut file, &mut nonce)? {
            break;
        }
        let len = u32::from_be_bytes(len) as usize;
        if len == 0 || len > MAX_CIPHERTEXT {
            return Err(RecordingError::Corrupt);
        }
        ct.resize(len, 0);
        if !read_full(&mut file, &mut ct)? {
            break; // torn by a crash mid-write
        }
        let aad = chunk_aad(&file_id, index);
        let plain = cipher
            .decrypt(Nonce::from_slice(&nonce), Payload { msg: &ct, aad: &aad })
            .map_err(|_| RecordingError::Corrupt)?;
        let take = plain.len().min((limit - total) as usize);
        f(&plain[..take])?;
        total += take as u64;
        index += 1;
    }
    Ok(total)
}

/// Fill `buf`; false at a clean or torn end of file.
fn read_full(r: &mut impl Read, buf: &mut [u8]) -> io::Result<bool> {
    let mut read = 0;
    while read < buf.len() {
        match r.read(&mut buf[read..])? {
            0 => return Ok(false),
            n => read += n,
        }
    }
    Ok(true)
}

pub fn wav_header(pcm_len: u64) -> [u8; 44] {
    let data = pcm_len.min(u32::MAX as u64 - 36) as u32;
    let mut h = [0u8; 44];
    h[0..4].copy_from_slice(b"RIFF");
    h[4..8].copy_from_slice(&(36 + data).to_le_bytes());
    h[8..12].copy_from_slice(b"WAVE");
    h[12..16].copy_from_slice(b"fmt ");
    h[16..20].copy_from_slice(&16u32.to_le_bytes());
    h[20..22].copy_from_slice(&1u16.to_le_bytes()); // PCM
    h[22..24].copy_from_slice(&1u16.to_le_bytes()); // mono
    h[24..28].copy_from_slice(&SAMPLE_RATE.to_le_bytes());
    h[28..32].copy_from_slice(&(SAMPLE_RATE * 2).to_le_bytes());
    h[32..34].copy_from_slice(&2u16.to_le_bytes());
    h[34..36].copy_from_slice(&16u16.to_le_bytes());
    h[36..40].copy_from_slice(b"data");
    h[40..44].copy_from_slice(&data.to_le_bytes());
    h
}

/// A recording as the WAV file the API expects (`audio/wav`), decrypted on the fly.
#[derive(Clone)]
pub struct WavSource {
    path: PathBuf,
    key: RecordingKey,
    pcm_len: u64,
}

impl WavSource {
    /// Checks every chunk first, so the upload's length is known and a damaged file never
    /// starts an upload.
    pub fn open(path: &Path, key: &RecordingKey) -> Result<Self, RecordingError> {
        let pcm_len = read_chunks(path, key, u64::MAX, |_| Ok(()))?;
        Ok(Self { path: path.into(), key: key.clone(), pcm_len })
    }

    pub fn len(&self) -> u64 {
        44 + self.pcm_len
    }

    pub fn is_empty(&self) -> bool {
        self.pcm_len == 0
    }

    pub fn read_all(&self) -> Result<Vec<u8>, RecordingError> {
        let mut out = Vec::with_capacity(self.len() as usize);
        out.extend_from_slice(&wav_header(self.pcm_len));
        read_chunks(&self.path, &self.key, self.pcm_len, |p| {
            out.extend_from_slice(p);
            Ok(())
        })?;
        Ok(out)
    }

    /// The WAV as a streamed request body; decryption runs on a blocking thread.
    pub fn into_body(self) -> reqwest::Body {
        let (tx, rx) = tokio::sync::mpsc::channel::<io::Result<Vec<u8>>>(8);
        let header = wav_header(self.pcm_len).to_vec();
        tokio::task::spawn_blocking(move || {
            if tx.blocking_send(Ok(header)).is_err() {
                return;
            }
            let result = read_chunks(&self.path, &self.key, self.pcm_len, |p| {
                tx.blocking_send(Ok(p.to_vec())).map_err(|_| io::Error::other("upload stopped"))
            });
            if let Err(e) = result {
                let _ = tx.blocking_send(Err(io::Error::other(e.to_string())));
            }
        });
        let stream =
            futures_util::stream::unfold(rx, |mut rx| async move { rx.recv().await.map(|item| (item, rx)) });
        reqwest::Body::wrap_stream(stream)
    }
}

/// A recording on disk waiting for its upload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingRecording {
    pub session_id: Uuid,
    pub audio_path: PathBuf,
    pub manifest_path: PathBuf,
    pub finished: bool,
}

impl PendingRecording {
    pub fn delete(&self) {
        let _ = fs::remove_file(&self.audio_path);
        let _ = fs::remove_file(&self.manifest_path);
    }
}

/// Recordings in `dir` that belong to a session, oldest first. With `include_unfinished`
/// (at app start, when nothing records) a recording a crash left open counts too, and one
/// that never went live is deleted.
pub fn pending(dir: &Path, include_unfinished: bool) -> Vec<PendingRecording> {
    let Ok(entries) = fs::read_dir(dir) else { return vec![] };
    let mut out: Vec<(u64, PendingRecording)> = vec![];
    for entry in entries.flatten() {
        let manifest_path = entry.path();
        if manifest_path.extension().and_then(|e| e.to_str()) != Some(MANIFEST_EXT) {
            continue;
        }
        let audio_path = manifest_path.with_extension(AUDIO_EXT);
        let manifest: Option<Manifest> =
            fs::read(&manifest_path).ok().and_then(|b| serde_json::from_slice(&b).ok());
        let Some(manifest) = manifest else {
            if include_unfinished {
                let _ = fs::remove_file(&manifest_path);
                let _ = fs::remove_file(&audio_path);
            }
            continue;
        };
        if !manifest.finished && !include_unfinished {
            continue; // being recorded right now
        }
        match manifest.session_id {
            Some(session_id) if audio_path.exists() => out.push((
                manifest.started_at,
                PendingRecording { session_id, audio_path, manifest_path, finished: manifest.finished },
            )),
            _ if include_unfinished => {
                let _ = fs::remove_file(&manifest_path);
                let _ = fs::remove_file(&audio_path);
            }
            _ => {}
        }
    }
    out.sort_by_key(|(t, _)| *t);
    out.into_iter().map(|(_, p)| p).collect()
}

/// Delete every local recording (account deletion).
pub fn delete_all(dir: &Path) {
    if let Ok(entries) = fs::read_dir(dir) {
        for entry in entries.flatten() {
            let ext = entry.path().extension().and_then(|e| e.to_str()).map(str::to_owned);
            if matches!(ext.as_deref(), Some(AUDIO_EXT | MANIFEST_EXT))
                || entry.path().to_string_lossy().ends_with(".json.tmp")
            {
                let _ = fs::remove_file(entry.path());
            }
        }
    }
}

/// The three API calls an upload needs; [`ApiClient`] in the app, a fake in tests.
pub trait MeAudioApi: Send + Sync {
    fn upload_url(&self, session_id: Uuid) -> BoxFuture<'_, Result<PresignedUpload, ApiError>>;
    fn put(&self, upload: PresignedUpload, wav: WavSource) -> BoxFuture<'_, Result<(), ApiError>>;
    fn complete(&self, session_id: Uuid) -> BoxFuture<'_, Result<(), ApiError>>;
}

impl MeAudioApi for ApiClient {
    fn upload_url(&self, session_id: Uuid) -> BoxFuture<'_, Result<PresignedUpload, ApiError>> {
        Box::pin(async move {
            let body = serde_json::json!({ "content_type": "audio/wav" });
            let r = self.post(&format!("/sessions/{session_id}/me-audio"), &body).await?;
            serde_json::from_value(r["upload"].clone()).map_err(|e| ApiError::Invalid(format!("upload: {e}")))
        })
    }

    fn put(&self, upload: PresignedUpload, wav: WavSource) -> BoxFuture<'_, Result<(), ApiError>> {
        Box::pin(async move {
            let len = wav.len();
            self.put_presigned(&upload, wav.into_body(), len).await
        })
    }

    fn complete(&self, session_id: Uuid) -> BoxFuture<'_, Result<(), ApiError>> {
        Box::pin(async move {
            self.post(&format!("/sessions/{session_id}/me-audio/complete"), &serde_json::Value::Null)
                .await
                .map(|_| ())
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "state", content = "message", rename_all = "snake_case")]
pub enum UploadOutcome {
    /// Uploaded and completed; the local copy is gone.
    Uploaded,
    /// The API will never take it (no consent, too late, no such session, unreadable);
    /// the local copy is gone.
    Discarded(String),
    /// Try again later (network, the session still live, an auth problem); kept on disk.
    Kept(String),
}

/// What an API error means for a recording.
pub fn classify(e: &ApiError) -> UploadOutcome {
    match e {
        ApiError::Status { status: 409, detail } if detail.starts_with("live:") => {
            UploadOutcome::Kept(e.to_string())
        }
        // 409: no consent, already transcribed or given up on (the 10-minute wait passed);
        // 404: the session is gone; 413: too long to transcribe; 422: refused as sent.
        ApiError::Status { status: 404 | 409 | 413 | 422, .. } => UploadOutcome::Discarded(e.to_string()),
        _ => UploadOutcome::Kept(e.to_string()),
    }
}

/// Upload one recording, deleting it unless it should be tried again.
pub async fn upload_one(
    api: &dyn MeAudioApi,
    key: Option<&RecordingKey>,
    rec: &PendingRecording,
) -> UploadOutcome {
    let Some(key) = key else {
        rec.delete();
        return UploadOutcome::Discarded("this install's recording key is gone".into());
    };
    let wav = match WavSource::open(&rec.audio_path, key) {
        Ok(w) if !w.is_empty() => w,
        Ok(_) => {
            rec.delete();
            return UploadOutcome::Discarded("the recording is empty".into());
        }
        Err(RecordingError::Io(e)) => return UploadOutcome::Kept(e.to_string()),
        Err(e) => {
            rec.delete();
            return UploadOutcome::Discarded(e.to_string());
        }
    };
    let result = async {
        let upload = api.upload_url(rec.session_id).await?;
        // A refused PUT (an expired URL, the store down) gets a new URL next time.
        api.put(upload, wav).await.map_err(|e| match e {
            ApiError::Status { status, detail } => {
                ApiError::Status { status: 502, detail: format!("{detail} ({status})") }
            }
            other => other,
        })?;
        api.complete(rec.session_id).await
    }
    .await;
    let outcome = match result {
        Ok(()) => UploadOutcome::Uploaded,
        Err(e) => classify(&e),
    };
    if !matches!(outcome, UploadOutcome::Kept(_)) {
        rec.delete();
    }
    outcome
}

/// Upload every waiting recording, retrying the ones kept after each delay in `schedule`.
/// `report` sees every attempt's outcome. Returns the last outcome per session.
pub async fn upload_pending(
    api: &dyn MeAudioApi,
    store: &dyn SecretStore,
    dir: &Path,
    include_unfinished: bool,
    schedule: &[Duration],
    report: &(dyn Fn(Uuid, &UploadOutcome) + Send + Sync),
) -> Vec<(Uuid, UploadOutcome)> {
    let key = match RecordingKey::load(store) {
        Ok(k) => k,
        Err(e) => {
            tracing::warn!("recording key: {e}");
            return vec![]; // the store is unreachable: keep everything for next time
        }
    };
    let mut waiting = pending(dir, include_unfinished);
    let mut last: Vec<(Uuid, UploadOutcome)> = vec![];
    let mut delays = std::iter::once(Duration::ZERO).chain(schedule.iter().copied());
    while !waiting.is_empty() {
        let Some(delay) = delays.next() else { break };
        tokio::time::sleep(delay).await;
        let mut kept = vec![];
        for rec in waiting {
            let outcome = upload_one(api, key.as_ref(), &rec).await;
            report(rec.session_id, &outcome);
            last.retain(|(sid, _)| *sid != rec.session_id);
            last.push((rec.session_id, outcome.clone()));
            if matches!(outcome, UploadOutcome::Kept(_)) {
                kept.push(rec);
            }
        }
        waiting = kept;
    }
    last
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::MemorySecretStore;

    fn tone(n: usize) -> Vec<f32> {
        (0..n).map(|i| ((i as f32) * 0.05).sin() * 0.5).collect()
    }

    fn record(dir: &Path, key: &RecordingKey, sid: Option<Uuid>, samples: &[f32]) -> Recorder {
        let mut rec = Recorder::start(dir, key.clone()).unwrap();
        if let Some(sid) = sid {
            (rec.session_binder().unwrap())(sid);
        }
        for tick in samples.chunks(160) {
            rec.on_audio(0, tick);
        }
        rec.finish();
        rec
    }

    fn pcm(samples: &[f32]) -> Vec<u8> {
        samples.iter().flat_map(|s| ((s.clamp(-1.0, 1.0) * 32767.0).round() as i16).to_le_bytes()).collect()
    }

    #[test]
    fn key_is_created_once_and_kept_in_the_store() {
        let store = MemorySecretStore::default();
        assert!(RecordingKey::load(&store).unwrap().is_none());
        let a = RecordingKey::load_or_create(&store).unwrap();
        let b = RecordingKey::load_or_create(&store).unwrap();
        assert_eq!(a.0, b.0);
        assert_eq!(store.get(RECORDING_KEY).unwrap().unwrap().len(), 64);
        store.set(RECORDING_KEY, "zz").unwrap();
        assert!(RecordingKey::load(&store).is_err());
    }

    #[test]
    fn a_recording_round_trips_as_wav_and_is_not_plaintext_on_disk() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        let sid = Uuid::from_u128(7);
        let samples = tone(16_000 * 2 + 480); // 2.03 s: two full chunks and a partial one
        record(dir.path(), &key, Some(sid), &samples);

        let [rec] = pending(dir.path(), false).try_into().unwrap();
        assert_eq!(rec.session_id, sid);
        assert!(rec.finished);
        let raw = fs::read(&rec.audio_path).unwrap();
        let plain = pcm(&samples);
        assert!(!raw.windows(64).any(|w| w == &plain[1000..1064]), "plaintext on disk");

        let wav = WavSource::open(&rec.audio_path, &key).unwrap();
        assert_eq!(wav.len(), 44 + plain.len() as u64);
        let bytes = wav.read_all().unwrap();
        assert_eq!(&bytes[..44], &wav_header(plain.len() as u64));
        assert_eq!(&bytes[44..], &plain[..]);
        let mut reader = hound::WavReader::new(io::Cursor::new(bytes)).unwrap();
        assert_eq!(reader.spec().sample_rate, 16_000);
        assert_eq!(reader.samples::<i16>().count(), samples.len());
    }

    #[test]
    fn the_wrong_key_or_an_altered_file_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        let other = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        record(dir.path(), &key, Some(Uuid::from_u128(1)), &tone(40_000));
        let [rec] = pending(dir.path(), false).try_into().unwrap();
        assert!(matches!(WavSource::open(&rec.audio_path, &other), Err(RecordingError::Corrupt)));

        let mut raw = fs::read(&rec.audio_path).unwrap();
        let first_chunk = HEADER_LEN + 4 + 12;
        raw[first_chunk + 5] ^= 1;
        fs::write(&rec.audio_path, &raw).unwrap();
        assert!(matches!(WavSource::open(&rec.audio_path, &key), Err(RecordingError::Corrupt)));
    }

    #[test]
    fn chunks_cannot_be_reordered() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        record(dir.path(), &key, Some(Uuid::from_u128(1)), &tone(CHUNK_BYTES)); // two chunks
        let [rec] = pending(dir.path(), false).try_into().unwrap();
        let raw = fs::read(&rec.audio_path).unwrap();
        let chunk = 4 + 12 + MAX_CIPHERTEXT;
        let (head, rest) = raw.split_at(HEADER_LEN);
        let swapped = [head, &rest[chunk..2 * chunk], &rest[..chunk]].concat();
        fs::write(&rec.audio_path, swapped).unwrap();
        assert!(matches!(WavSource::open(&rec.audio_path, &key), Err(RecordingError::Corrupt)));
    }

    #[test]
    fn a_torn_last_chunk_after_a_crash_is_dropped() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        let samples = tone(CHUNK_BYTES); // exactly two full chunks
        record(dir.path(), &key, Some(Uuid::from_u128(1)), &samples);
        let [rec] = pending(dir.path(), false).try_into().unwrap();
        let raw = fs::read(&rec.audio_path).unwrap();
        fs::write(&rec.audio_path, &raw[..raw.len() - 100]).unwrap();
        let wav = WavSource::open(&rec.audio_path, &key).unwrap();
        assert_eq!(wav.len(), 44 + CHUNK_BYTES as u64); // the first second survives
    }

    #[test]
    fn a_new_session_mid_call_cuts_the_recording() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        let (a, b) = (Uuid::from_u128(1), Uuid::from_u128(2));
        let mut rec = Recorder::start(dir.path(), key.clone()).unwrap();
        let bind = rec.session_binder().unwrap();
        rec.on_audio(0, &tone(8_000));
        bind(a);
        bind(a); // a resume of the same session changes nothing
        rec.on_audio(0, &tone(8_000));
        bind(b);
        rec.on_audio(0, &tone(4_000));
        rec.finish();
        let found = pending(dir.path(), false);
        let lens: Vec<(Uuid, u64)> = found
            .iter()
            .map(|p| (p.session_id, WavSource::open(&p.audio_path, &key).unwrap().len() - 44))
            .collect();
        assert_eq!(lens.len(), 2);
        assert!(lens.contains(&(a, 32_000)) && lens.contains(&(b, 8_000)), "{lens:?}");
    }

    #[test]
    fn without_consent_nothing_is_recorded_and_no_key_is_made() {
        let dir = tempfile::tempdir().unwrap();
        let store = MemorySecretStore::default();
        let mut sink = sink_for_session(false, &dir.path().join("me-audio"), &store).unwrap();
        assert!(sink.session_binder().is_none());
        sink.on_audio(0, &tone(40_000));
        sink.finish();
        assert!(!dir.path().join("me-audio").exists());
        assert!(store.get(RECORDING_KEY).unwrap().is_none());

        let mut sink = sink_for_session(true, &dir.path().join("me-audio"), &store).unwrap();
        (sink.session_binder().unwrap())(Uuid::from_u128(5));
        sink.on_audio(0, &tone(20_000));
        sink.finish();
        assert_eq!(pending(&dir.path().join("me-audio"), false).len(), 1);
        assert!(store.get(RECORDING_KEY).unwrap().is_some());
    }

    #[test]
    fn a_recording_that_never_went_live_is_deleted() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        record(dir.path(), &key, None, &tone(20_000));
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 0);
    }

    #[test]
    fn unfinished_recordings_wait_until_app_start() {
        let dir = tempfile::tempdir().unwrap();
        let key = RecordingKey::load_or_create(&MemorySecretStore::default()).unwrap();
        let mut rec = Recorder::start(dir.path(), key.clone()).unwrap();
        (rec.session_binder().unwrap())(Uuid::from_u128(3));
        rec.on_audio(0, &tone(40_000));
        // Still recording (or the app crashed): not offered while running...
        assert!(pending(dir.path(), false).is_empty());
        // ...but at the next start it is, with what reached the disk.
        let [p] = pending(dir.path(), true).try_into().unwrap();
        assert!(!p.finished);
        assert_eq!(WavSource::open(&p.audio_path, &key).unwrap().len(), 44 + 64_000);
        rec.finish();
    }

    /// The API, scripted per call.
    #[derive(Default)]
    struct FakeApi {
        url_results: Mutex<Vec<Result<(), ApiError>>>,
        put_results: Mutex<Vec<Result<(), ApiError>>>,
        complete_results: Mutex<Vec<Result<(), ApiError>>>,
        uploaded: Mutex<Vec<(Uuid, Vec<u8>)>>,
        completed: Mutex<Vec<Uuid>>,
    }

    fn next(results: &Mutex<Vec<Result<(), ApiError>>>) -> Result<(), ApiError> {
        let mut r = results.lock().unwrap();
        if r.is_empty() { Ok(()) } else { r.remove(0) }
    }

    impl MeAudioApi for FakeApi {
        fn upload_url(&self, sid: Uuid) -> BoxFuture<'_, Result<PresignedUpload, ApiError>> {
            Box::pin(async move {
                next(&self.url_results)?;
                Ok(PresignedUpload {
                    method: "PUT".into(),
                    url: format!("s3://{sid}"),
                    headers: Default::default(),
                })
            })
        }

        fn put(&self, upload: PresignedUpload, wav: WavSource) -> BoxFuture<'_, Result<(), ApiError>> {
            Box::pin(async move {
                next(&self.put_results)?;
                let sid = Uuid::parse_str(upload.url.trim_start_matches("s3://")).unwrap();
                self.uploaded.lock().unwrap().push((sid, wav.read_all().unwrap()));
                Ok(())
            })
        }

        fn complete(&self, sid: Uuid) -> BoxFuture<'_, Result<(), ApiError>> {
            Box::pin(async move {
                next(&self.complete_results)?;
                self.completed.lock().unwrap().push(sid);
                Ok(())
            })
        }
    }

    fn status(status: u16, detail: &str) -> ApiError {
        ApiError::Status { status, detail: detail.into() }
    }

    struct Setup {
        dir: tempfile::TempDir,
        store: MemorySecretStore,
        key: RecordingKey,
    }

    fn setup() -> Setup {
        let store = MemorySecretStore::default();
        let key = RecordingKey::load_or_create(&store).unwrap();
        Setup { dir: tempfile::tempdir().unwrap(), store, key }
    }

    async fn run(s: &Setup, api: &FakeApi, schedule: &[Duration]) -> Vec<(Uuid, UploadOutcome)> {
        upload_pending(api, &s.store, s.dir.path(), true, schedule, &|_, _| {}).await
    }

    #[tokio::test]
    async fn a_successful_upload_completes_and_deletes_the_local_copy() {
        let s = setup();
        let sid = Uuid::from_u128(9);
        let samples = tone(20_000);
        record(s.dir.path(), &s.key, Some(sid), &samples);
        let api = FakeApi::default();
        assert_eq!(run(&s, &api, &[]).await, vec![(sid, UploadOutcome::Uploaded)]);
        let uploaded = api.uploaded.lock().unwrap();
        assert_eq!(uploaded[0].0, sid);
        assert_eq!(&uploaded[0].1[44..], &pcm(&samples)[..]);
        assert_eq!(*api.completed.lock().unwrap(), vec![sid]);
        assert_eq!(fs::read_dir(s.dir.path()).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn network_failures_keep_the_recording_and_retry_succeeds() {
        let s = setup();
        let sid = Uuid::from_u128(10);
        record(s.dir.path(), &s.key, Some(sid), &tone(20_000));
        let api = FakeApi::default();
        *api.put_results.lock().unwrap() = vec![Err(ApiError::Network("s3".into()))];
        *api.complete_results.lock().unwrap() = vec![Err(status(503, "busy"))];
        let attempts = Mutex::new(vec![]);
        let out = upload_pending(&api, &s.store, s.dir.path(), true, &[Duration::ZERO; 3], &|_, o| {
            attempts.lock().unwrap().push(o.clone())
        })
        .await;
        assert_eq!(out, vec![(sid, UploadOutcome::Uploaded)]);
        let attempts = attempts.into_inner().unwrap();
        assert_eq!(attempts.len(), 3);
        assert!(
            matches!(attempts[0], UploadOutcome::Kept(_)) && matches!(attempts[1], UploadOutcome::Kept(_))
        );
        assert_eq!(api.uploaded.lock().unwrap().len(), 2); // the second PUT, then the third
        assert_eq!(fs::read_dir(s.dir.path()).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn a_failed_upload_stays_for_the_next_app_start() {
        let s = setup();
        let sid = Uuid::from_u128(11);
        record(s.dir.path(), &s.key, Some(sid), &tone(20_000));
        let api = FakeApi::default();
        *api.url_results.lock().unwrap() = vec![Err(ApiError::Network("down".into())); 2];
        let out = run(&s, &api, &[Duration::ZERO]).await; // two attempts, both fail
        assert!(matches!(out[0].1, UploadOutcome::Kept(_)));
        assert_eq!(pending(s.dir.path(), false).len(), 1);
        // Next start: the API is back.
        assert_eq!(run(&s, &api, &[]).await, vec![(sid, UploadOutcome::Uploaded)]);
        assert!(pending(s.dir.path(), true).is_empty());
    }

    #[tokio::test]
    async fn a_live_session_waits_and_refusals_delete() {
        let s = setup();
        let live = Uuid::from_u128(12);
        record(s.dir.path(), &s.key, Some(live), &tone(20_000));
        let api = FakeApi::default();
        *api.url_results.lock().unwrap() = vec![Err(status(409, "live: the session has not ended yet"))];
        let out = run(&s, &api, &[]).await;
        assert!(matches!(out[0].1, UploadOutcome::Kept(_)));
        assert_eq!(pending(s.dir.path(), true).len(), 1);

        for (code, detail) in
            [(409, "the audio is already none"), (404, "no such session"), (413, "too large")]
        {
            *api.url_results.lock().unwrap() = vec![];
            *api.complete_results.lock().unwrap() = vec![Err(status(code, detail))];
            let out = run(&s, &api, &[Duration::ZERO]).await;
            assert!(matches!(&out[0].1, UploadOutcome::Discarded(m) if m.contains(detail)), "{out:?}");
            assert!(pending(s.dir.path(), true).is_empty());
            record(s.dir.path(), &s.key, Some(live), &tone(20_000));
        }
    }

    #[tokio::test]
    async fn an_unreadable_recording_or_a_lost_key_is_discarded() {
        let s = setup();
        record(s.dir.path(), &s.key, Some(Uuid::from_u128(13)), &tone(20_000));
        let [rec] = pending(s.dir.path(), true).try_into().unwrap();
        fs::write(&rec.audio_path, b"CPMA\x01garbage-garbage-garbage-garbage").unwrap();
        let api = FakeApi::default();
        let out = run(&s, &api, &[]).await;
        assert!(matches!(out[0].1, UploadOutcome::Discarded(_)));
        assert!(api.uploaded.lock().unwrap().is_empty());

        record(s.dir.path(), &s.key, Some(Uuid::from_u128(14)), &tone(20_000));
        s.store.delete(RECORDING_KEY).unwrap();
        let out = run(&s, &api, &[]).await;
        assert!(matches!(&out[0].1, UploadOutcome::Discarded(m) if m.contains("key")));
        assert!(pending(s.dir.path(), true).is_empty());
    }

    #[test]
    fn classify_live_conflicts_apart_from_final_ones() {
        assert!(matches!(classify(&status(409, "live: x")), UploadOutcome::Kept(_)));
        assert!(matches!(
            classify(&status(409, "the audio is already transcribed")),
            UploadOutcome::Discarded(_)
        ));
        assert!(matches!(classify(&ApiError::Unauthorized), UploadOutcome::Kept(_)));
        assert!(matches!(classify(&status(500, "x")), UploadOutcome::Kept(_)));
    }
}
