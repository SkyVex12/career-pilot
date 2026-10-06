//! The gateway connection (ARCHITECTURE.md, section 1): one WebSocket per live session, kept
//! alive across drops.
//!
//! One task owns the socket and all session state: the session id, the last `seq` processed,
//! per-channel `frame_seq` counters and the resend ring. Audio frames and user actions come in
//! as [`Command`]s; server messages and connection changes go out as [`ClientEvent`]s.
//!
//! - First message: `session.start`, or `session.resume` with the last `seq` after a drop.
//!   Audio waits for `session.ready` on a start; after a resume the ring is resent at once
//!   and the gateway drops what it already has.
//! - Messages replayed on resume with a `seq` already processed are dropped here.
//! - WebSocket pings every 30 s keep the load balancer from closing a quiet call.
//! - Tokens: [`TokenProvider::current`] for the upgrade's `Authorization` header;
//!   `refresh` on `auth.expiring`, two minutes before a known expiry, and after a `4401`.
//! - Every close goes through [`close::on_close`]; reconnects follow [`Backoff`].

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use copilot_protocol::audio::{CHANNEL_ME, CHANNEL_THEM, encode_frame};
use copilot_protocol::{
    AudioFormat, AudioFrameHeader, AuthRefresh, ClientMessage, Consent, Mode, ServerMessage, SessionEnd,
    SessionResume, SessionStart, parse_server,
};
use futures_util::{SinkExt, StreamExt};
use rand::SeedableRng;
use rand::rngs::StdRng;
use serde::Serialize;
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};
use uuid::Uuid;

use super::backoff::Backoff;
use super::close::{self, CloseAction, CloseContext, Delay, StopReason};
use super::ring::{FrameRing, RingFrame};
use crate::audio::OutFrame;
use crate::auth::{AuthError, Token, TokenProvider};

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;

pub const PING_INTERVAL: Duration = Duration::from_secs(30);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// How long `session.ready` may take after `session.start`.
const READY_TIMEOUT: Duration = Duration::from_secs(10);
/// A connection that stays up this long counts as working: the backoff starts over.
const STABLE_AFTER: Duration = Duration::from_secs(3);
/// Refresh this long before a token's known expiry (the gateway warns at two minutes).
const REFRESH_LEAD: Duration = Duration::from_secs(150);
/// A resume has no acknowledgement: it counts as accepted on the first server message, or
/// when the gateway has not rejected it (4408, 4409, 4500) within this.
const RESUME_SETTLE: Duration = Duration::from_millis(500);
/// After `session.end`, how long to wait for the gateway's `1000`.
const END_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone)]
pub struct StartParams {
    pub mode: Mode,
    pub job_id: Option<Uuid>,
    pub meeting_title: Option<String>,
    pub platform: String,
    pub client_version: String,
    pub store_transcript: bool,
}

impl StartParams {
    fn message(&self) -> ClientMessage {
        ClientMessage::SessionStart(SessionStart {
            mode: self.mode,
            job_id: self.job_id,
            meeting_title: self.meeting_title.clone().filter(|t| !t.trim().is_empty()),
            platform: self.platform.clone(),
            client_version: self.client_version.clone(),
            consent: Consent { store_transcript: self.store_transcript },
            audio: AudioFormat { sample_rate: 16_000, encoding: "pcm16".into() },
        })
    }
}

#[derive(Debug, Clone)]
pub struct ClientConfig {
    /// `ws://` or `wss://` URL of `/v1/session`.
    pub url: String,
    pub start: StartParams,
    pub ping_interval: Duration,
    pub ring_seconds: u32,
}

impl ClientConfig {
    pub fn new(url: impl Into<String>, start: StartParams) -> Self {
        Self { url: url.into(), start, ping_interval: PING_INTERVAL, ring_seconds: 10 }
    }
}

#[derive(Debug)]
pub enum Command {
    Frame(OutFrame),
    Send(ClientMessage),
    /// Send `session.end` and stop once the gateway closes.
    End,
    /// Stop now, leaving the session resumable (the gateway ends it after 120 s).
    Shutdown,
    /// Test hook: drop the socket without a close handshake, as a network failure would.
    DropConnection,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum ConnectionState {
    Connecting {
        attempt: usize,
    },
    /// Audio is flowing. `resumed` is false for a new session.
    Live {
        session_id: Uuid,
        resumed: bool,
    },
    Reconnecting {
        attempt: usize,
        in_ms: u64,
        close_code: Option<u16>,
    },
    Stopped {
        reason: StopReason,
    },
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClientEvent {
    Connection(ConnectionState),
    /// A server message, once per `seq`.
    Server {
        message: ServerMessage,
    },
    /// The socket closed (before the reconnect decision).
    Closed {
        code: Option<u16>,
        reason: String,
    },
    Warning {
        message: String,
    },
}

/// Counters for the debug panel and the tests.
#[derive(Debug, Clone, Default, Serialize)]
pub struct ClientStats {
    pub connects: usize,
    pub resumes: usize,
    pub new_sessions: usize,
    pub frames_sent: u64,
    pub frames_resent: u64,
    pub frames_dropped: u64,
    pub pings_sent: u64,
    pub token_refreshes: u64,
    pub close_codes: Vec<Option<u16>>,
    pub session_ids: Vec<Uuid>,
}

#[derive(Clone)]
pub struct ClientHandle {
    tx: mpsc::Sender<Command>,
    pub stats: Arc<Mutex<ClientStats>>,
}

impl ClientHandle {
    /// From the audio thread: never blocks; a full queue drops the frame.
    pub fn send_frame(&self, frame: OutFrame) {
        if self.tx.try_send(Command::Frame(frame)).is_err() {
            self.stats.lock().unwrap().frames_dropped += 1;
        }
    }

    pub async fn send(&self, message: ClientMessage) {
        let _ = self.tx.send(Command::Send(message)).await;
    }

    pub fn try_send(&self, command: Command) -> bool {
        self.tx.try_send(command).is_ok()
    }

    pub async fn command(&self, command: Command) {
        let _ = self.tx.send(command).await;
    }

    pub fn stats(&self) -> ClientStats {
        self.stats.lock().unwrap().clone()
    }
}

pub fn spawn(
    config: ClientConfig,
    tokens: Arc<dyn TokenProvider>,
    events: mpsc::UnboundedSender<ClientEvent>,
) -> (ClientHandle, tokio::task::JoinHandle<StopReason>) {
    let (tx, rx) = mpsc::channel(2048);
    let stats = Arc::new(Mutex::new(ClientStats::default()));
    let client = Client {
        ring: FrameRing::new(config.ring_seconds),
        config,
        tokens,
        events,
        rx,
        stats: stats.clone(),
        session_id: None,
        last_seq: 0,
        frame_seq: [0, 0],
        capture_base_ms: 0,
        last_capture_ms: 0,
        ending: false,
        backoff: Backoff::new(),
        rng: StdRng::from_os_rng(),
        token_expires_at: None,
    };
    let task = tokio::spawn(client.run());
    (ClientHandle { tx, stats }, task)
}

struct Client {
    config: ClientConfig,
    tokens: Arc<dyn TokenProvider>,
    events: mpsc::UnboundedSender<ClientEvent>,
    rx: mpsc::Receiver<Command>,
    stats: Arc<Mutex<ClientStats>>,
    session_id: Option<Uuid>,
    last_seq: u64,
    frame_seq: [u32; 2],
    ring: FrameRing,
    /// Stream position where the current session started (non-zero after a 4408 restart).
    capture_base_ms: u32,
    last_capture_ms: u32,
    ending: bool,
    backoff: Backoff,
    rng: StdRng,
    token_expires_at: Option<SystemTime>,
}

/// How one connection ended.
enum Outcome {
    Closed {
        code: Option<u16>,
        reason: String,
        retry_after_ms: Option<u64>,
    },
    /// Shutdown, or every handle dropped.
    Shutdown,
}

impl Client {
    fn emit(&self, event: ClientEvent) {
        let _ = self.events.send(event);
    }

    fn state(&self, state: ConnectionState) {
        self.emit(ClientEvent::Connection(state));
    }

    fn stats(&self) -> std::sync::MutexGuard<'_, ClientStats> {
        self.stats.lock().unwrap()
    }

    async fn run(mut self) -> StopReason {
        let mut delay = Duration::ZERO;
        let mut refreshed_after_401 = false;
        let mut last_close: Option<u16> = None;
        loop {
            if !delay.is_zero() {
                self.state(ConnectionState::Reconnecting {
                    attempt: self.backoff.attempt(),
                    in_ms: delay.as_millis() as u64,
                    close_code: last_close,
                });
                if self.wait_offline(delay).await {
                    return self.stop(StopReason::Ended);
                }
            }
            if self.ending && self.session_id.is_none() {
                return self.stop(StopReason::Ended);
            }
            self.state(ConnectionState::Connecting { attempt: self.backoff.attempt() });
            let token = match self.tokens.current().await {
                Ok(t) => t,
                Err(e) => {
                    self.emit(ClientEvent::Warning { message: e.to_string() });
                    return self.stop(StopReason::Unauthorized);
                }
            };
            self.token_expires_at = token.expires_at;
            let ws = match connect(&self.config.url, &token).await {
                Ok(ws) => ws,
                Err(e) => {
                    self.emit(ClientEvent::Warning { message: format!("connect failed: {e}") });
                    last_close = None;
                    delay = self.backoff.next_delay(&mut self.rng);
                    continue;
                }
            };
            self.stats().connects += 1;
            let outcome = self.serve(ws).await;
            let (code, retry_after_ms) = match outcome {
                Outcome::Shutdown => return self.stop(StopReason::Ended),
                Outcome::Closed { code, reason, retry_after_ms } => {
                    self.stats().close_codes.push(code);
                    self.emit(ClientEvent::Closed { code, reason });
                    (code, retry_after_ms)
                }
            };
            last_close = code;
            let ctx = CloseContext {
                has_session: self.session_id.is_some(),
                ending: self.ending,
                retry_after_ms,
                refreshed_after_401,
            };
            match close::on_close(code, ctx) {
                CloseAction::Stop(reason) => return self.stop(reason),
                CloseAction::Reconnect { resume, delay: d, refresh_token } => {
                    refreshed_after_401 = false;
                    if refresh_token {
                        match self.tokens.refresh().await {
                            Ok(t) => {
                                self.token_expires_at = t.expires_at;
                                self.stats().token_refreshes += 1;
                                refreshed_after_401 = true;
                            }
                            Err(e) => {
                                self.emit(ClientEvent::Warning { message: e.to_string() });
                                return self.stop(StopReason::Unauthorized);
                            }
                        }
                    }
                    if !resume {
                        self.new_session();
                    }
                    delay = match d {
                        Delay::Now if self.backoff.attempt() == 0 => {
                            self.backoff.next_delay(&mut self.rng);
                            Duration::ZERO
                        }
                        Delay::Now | Delay::Backoff => self.backoff.next_delay(&mut self.rng),
                        Delay::After(d) => d,
                    };
                }
            }
        }
    }

    fn stop(&self, reason: StopReason) -> StopReason {
        self.state(ConnectionState::Stopped { reason });
        reason
    }

    /// The next connection starts a new session: frame numbering and the ring start over.
    fn new_session(&mut self) {
        self.session_id = None;
        self.last_seq = 0;
        self.frame_seq = [0, 0];
        self.ring.clear();
        self.capture_base_ms = self.last_capture_ms;
    }

    /// Wait out a reconnect delay while still buffering audio. True if told to stop.
    async fn wait_offline(&mut self, delay: Duration) -> bool {
        let until = tokio::time::Instant::now() + delay;
        loop {
            tokio::select! {
                _ = tokio::time::sleep_until(until) => return false,
                cmd = self.rx.recv() => match cmd {
                    Some(Command::Frame(f)) => { self.buffer(f); }
                    Some(Command::Send(m)) => self.emit(ClientEvent::Warning {
                        message: format!("not connected; {} not sent", m.type_name()),
                    }),
                    Some(Command::End) => {
                        // Nothing to tell a gateway we cannot reach; it ends the session after
                        // the resume window.
                        self.ending = true;
                        return true;
                    }
                    Some(Command::Shutdown) | None => return true,
                    Some(Command::DropConnection) => {}
                }
            }
        }
    }

    /// Number and encode a frame and keep it in the ring.
    fn buffer(&mut self, f: OutFrame) -> Option<Vec<u8>> {
        let ch = usize::from(f.channel == CHANNEL_ME);
        self.last_capture_ms = self.last_capture_ms.max(f.capture_ms);
        let header = AudioFrameHeader {
            channel: if ch == 1 { CHANNEL_ME } else { CHANNEL_THEM },
            flags: f.flags,
            frame_seq: self.frame_seq[ch],
            capture_ms: f.capture_ms.saturating_sub(self.capture_base_ms),
        };
        let bytes = match encode_frame(&header, &f.audio_bytes()) {
            Ok(b) => b,
            Err(e) => {
                self.emit(ClientEvent::Warning { message: format!("frame not sent: {e}") });
                return None;
            }
        };
        self.frame_seq[ch] = self.frame_seq[ch].wrapping_add(1);
        self.ring.push(RingFrame {
            channel: header.channel,
            frame_seq: header.frame_seq,
            capture_ms: header.capture_ms,
            bytes: bytes.clone(),
            audio_samples: f.audio.len(),
        });
        Some(bytes)
    }

    async fn send_json(ws: &mut Ws, message: &ClientMessage) -> Result<(), String> {
        let text = message.to_json().map_err(|e| e.to_string())?;
        ws.send(Message::text(text)).await.map_err(|e| e.to_string())
    }

    async fn resend_ring(&mut self, ws: &mut Ws) -> Result<(), String> {
        let frames: Vec<Vec<u8>> = self.ring.iter().map(|f| f.bytes.clone()).collect();
        let n = frames.len() as u64;
        for bytes in frames {
            ws.send(Message::binary(bytes)).await.map_err(|e| e.to_string())?;
        }
        self.stats().frames_resent += n;
        Ok(())
    }

    async fn refresh_token(&mut self, ws: &mut Ws) {
        match self.tokens.refresh().await {
            Ok(Token { access_token, expires_at }) => {
                self.token_expires_at = expires_at;
                self.stats().token_refreshes += 1;
                let msg = ClientMessage::AuthRefresh(AuthRefresh { access_token });
                if let Err(e) = Self::send_json(ws, &msg).await {
                    self.emit(ClientEvent::Warning { message: format!("auth.refresh not sent: {e}") });
                }
            }
            Err(e @ AuthError::NotSignedIn(_)) | Err(e @ AuthError::Store(_)) => {
                self.emit(ClientEvent::Warning { message: format!("token refresh failed: {e}") });
            }
        }
    }

    fn announce_resumed(&self) {
        if let Some(session_id) = self.session_id {
            self.state(ConnectionState::Live { session_id, resumed: true });
        }
    }

    fn refresh_due(&self) -> Option<tokio::time::Instant> {
        let expires = self.token_expires_at?;
        let left = expires.duration_since(SystemTime::now()).unwrap_or_default();
        Some(tokio::time::Instant::now() + left.saturating_sub(REFRESH_LEAD))
    }

    async fn serve(&mut self, mut ws: Ws) -> Outcome {
        let resuming = self.session_id.is_some();
        let first = match self.session_id {
            Some(session_id) => {
                ClientMessage::SessionResume(SessionResume { session_id, last_seq: self.last_seq })
            }
            None => self.config.start.message(),
        };
        if let Err(e) = Self::send_json(&mut ws, &first).await {
            return Outcome::Closed { code: None, reason: e, retry_after_ms: None };
        }
        let mut live = false;
        if resuming {
            self.stats().resumes += 1;
            if let Err(e) = self.resend_ring(&mut ws).await {
                return Outcome::Closed { code: None, reason: e, retry_after_ms: None };
            }
            live = true;
        }
        // A resume is announced once the gateway has evidently accepted it.
        let mut announce_resume = resuming;
        let connected_at = Instant::now();
        let mut stable = false;
        let mut retry_after_ms = None;
        let mut ping = tokio::time::interval_at(
            tokio::time::Instant::now() + self.config.ping_interval,
            self.config.ping_interval,
        );
        let mut end_deadline: Option<tokio::time::Instant> = None;
        let far = tokio::time::Instant::now() + Duration::from_secs(365 * 24 * 3600);
        loop {
            let stable_at = tokio::time::Instant::from_std(connected_at + STABLE_AFTER);
            let ready_by = tokio::time::Instant::from_std(connected_at + READY_TIMEOUT);
            let refresh_at = self.refresh_due().unwrap_or(far);
            let settle_at = tokio::time::Instant::from_std(connected_at + RESUME_SETTLE);
            tokio::select! {
                incoming = ws.next() => match incoming {
                    Some(Ok(Message::Text(text))) => {
                        let message = match parse_server(&text) {
                            Ok(m) => m,
                            Err(e) => {
                                self.emit(ClientEvent::Warning { message: format!("ignored a server message: {e}") });
                                continue;
                            }
                        };
                        if message.seq() <= self.last_seq {
                            continue; // replayed on resume, already processed
                        }
                        self.last_seq = message.seq();
                        if announce_resume {
                            announce_resume = false;
                            self.announce_resumed();
                        }
                        if !stable {
                            stable = true;
                            self.backoff.reset();
                        }
                        match &message {
                            ServerMessage::SessionReady(ready) => {
                                self.session_id = Some(ready.session_id);
                                {
                                    let mut stats = self.stats();
                                    stats.new_sessions += 1;
                                    stats.session_ids.push(ready.session_id);
                                }
                                live = true;
                                self.state(ConnectionState::Live { session_id: ready.session_id, resumed: false });
                                // Audio captured since the start waited in the ring.
                                if let Err(e) = self.resend_ring(&mut ws).await {
                                    return Outcome::Closed { code: None, reason: e, retry_after_ms };
                                }
                            }
                            ServerMessage::ServerError(err) => {
                                if err.retry_after_ms.is_some() {
                                    retry_after_ms = err.retry_after_ms;
                                }
                            }
                            ServerMessage::AuthExpiring(_) => {
                                self.emit(ClientEvent::Server { message: message.clone() });
                                self.refresh_token(&mut ws).await;
                                continue;
                            }
                            _ => {}
                        }
                        self.emit(ClientEvent::Server { message });
                    }
                    Some(Ok(Message::Close(frame))) => {
                        let (code, reason) = frame.map_or((None, String::new()), |f| (Some(u16::from(f.code)), f.reason.to_string()));
                        // Let tungstenite finish the closing handshake.
                        let _ = tokio::time::timeout(Duration::from_millis(500), ws.close(None)).await;
                        return Outcome::Closed { code, reason, retry_after_ms };
                    }
                    Some(Ok(_)) => {} // binary, ping (answered by tungstenite), pong
                    Some(Err(e)) => return Outcome::Closed { code: None, reason: e.to_string(), retry_after_ms },
                    None => return Outcome::Closed { code: None, reason: "connection closed".into(), retry_after_ms },
                },
                cmd = self.rx.recv() => match cmd {
                    Some(Command::Frame(f)) => {
                        if let Some(bytes) = self.buffer(f) && live {
                            if let Err(e) = ws.send(Message::binary(bytes)).await {
                                return Outcome::Closed { code: None, reason: e.to_string(), retry_after_ms };
                            }
                            self.stats().frames_sent += 1;
                        }
                    }
                    Some(Command::Send(m)) => {
                        if !live {
                            self.emit(ClientEvent::Warning { message: format!("session not ready; {} not sent", m.type_name()) });
                        } else if let Err(e) = Self::send_json(&mut ws, &m).await {
                            return Outcome::Closed { code: None, reason: e, retry_after_ms };
                        }
                    }
                    Some(Command::End) => {
                        self.ending = true;
                        if self.session_id.is_none() {
                            let _ = ws.close(None).await;
                            return Outcome::Shutdown;
                        }
                        if let Err(e) = Self::send_json(&mut ws, &ClientMessage::SessionEnd(SessionEnd {})).await {
                            return Outcome::Closed { code: None, reason: e, retry_after_ms };
                        }
                        end_deadline = Some(tokio::time::Instant::now() + END_TIMEOUT);
                    }
                    Some(Command::Shutdown) | None => {
                        let _ = tokio::time::timeout(Duration::from_secs(1), ws.close(None)).await;
                        return Outcome::Shutdown;
                    }
                    Some(Command::DropConnection) => {
                        return Outcome::Closed { code: None, reason: "dropped (test hook)".into(), retry_after_ms };
                    }
                },
                _ = ping.tick() => {
                    if let Err(e) = ws.send(Message::Ping(Vec::new().into())).await {
                        return Outcome::Closed { code: None, reason: e.to_string(), retry_after_ms };
                    }
                    self.stats().pings_sent += 1;
                }
                _ = tokio::time::sleep_until(settle_at), if announce_resume => {
                    announce_resume = false;
                    self.announce_resumed();
                }
                _ = tokio::time::sleep_until(stable_at), if !stable && live => {
                    stable = true;
                    self.backoff.reset();
                }
                _ = tokio::time::sleep_until(ready_by), if !live => {
                    // No session.ready: treat as a dead connection and retry.
                    return Outcome::Closed { code: None, reason: "no session.ready".into(), retry_after_ms };
                }
                _ = tokio::time::sleep_until(refresh_at), if self.token_expires_at.is_some() => {
                    self.refresh_token(&mut ws).await;
                }
                _ = tokio::time::sleep_until(end_deadline.unwrap_or(far)), if end_deadline.is_some() => {
                    return Outcome::Shutdown;
                }
            }
        }
    }
}

async fn connect(url: &str, token: &Token) -> Result<Ws, String> {
    let mut request = url.into_client_request().map_err(|e| e.to_string())?;
    let auth = HeaderValue::from_str(&format!("Bearer {}", token.access_token)).map_err(|e| e.to_string())?;
    request.headers_mut().insert("Authorization", auth);
    match tokio::time::timeout(CONNECT_TIMEOUT, tokio_tungstenite::connect_async(request)).await {
        Ok(Ok((ws, _))) => Ok(ws),
        Ok(Err(e)) => Err(e.to_string()),
        Err(_) => Err("timed out".into()),
    }
}

/// End a session this client knows about but is not connected to (the 4409 "end the other
/// session" offer): resume it, send `session.end`, and wait for the close.
pub async fn end_session(
    url: &str,
    tokens: &dyn TokenProvider,
    session_id: Uuid,
) -> Result<Option<u16>, String> {
    let token = tokens.current().await.map_err(|e| e.to_string())?;
    let mut ws = connect(url, &token).await?;
    for m in [
        ClientMessage::SessionResume(SessionResume { session_id, last_seq: 0 }),
        ClientMessage::SessionEnd(SessionEnd {}),
    ] {
        Client::send_json(&mut ws, &m).await?;
    }
    let deadline = tokio::time::Instant::now() + END_TIMEOUT;
    loop {
        match tokio::time::timeout_at(deadline, ws.next()).await {
            Ok(Some(Ok(Message::Close(frame)))) => return Ok(frame.map(|f| u16::from(f.code))),
            Ok(Some(Ok(_))) => {}
            Ok(Some(Err(e))) => return Err(e.to_string()),
            Ok(None) => return Ok(None),
            Err(_) => return Err("timed out waiting for the gateway to end the session".into()),
        }
    }
}
