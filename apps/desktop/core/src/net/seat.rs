//! A guest's seat on someone else's live call (ARCHITECTURE.md, "Guest seats").
//!
//! One WebSocket to the gateway's `/v1/seat`, apart from any call of this app's own: it sends
//! no audio, starts nothing and ends nothing. It joins with `seat.join` and the last host `seq`
//! it processed, then passes every server message to the UI as the host's client does
//! ([`UiEvent::Server`]), so the overlay renders the call with the same reducer:
//!
//! - `seat.ready`, then `seat.snapshot` (the call so far, which replaces what was shown), then
//!   the host's messages in `seq` order. A host message at or below the last `seq` is a repeat
//!   and is dropped; the seat's own messages carry the current position and always pass.
//! - Notes go out as `seat.command`; `seat.command.result` says how each went.
//! - A dropped connection joins again with the last `seq`, on the reconnect backoff; the seat's
//!   own close codes (removed, closed, the call ended) stop it ([`close::on_seat_close`]).

use std::sync::Arc;
use std::time::Duration;

use copilot_protocol::{ClientMessage, SeatCommand, SeatCommandKind, SeatJoin, ServerMessage, parse_server};
use futures_util::{SinkExt, StreamExt};
use rand::SeedableRng;
use rand::rngs::StdRng;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode as WsCloseCode;
use uuid::Uuid;

use super::backoff::Backoff;
use super::client::{ConnectionState, PING_INTERVAL, Ws, connect};
use super::close::{self, CloseAction, Delay, StopReason};
use crate::auth::TokenProvider;
use crate::engine::{EventSink, UiEvent};

/// A connection that stays up this long counts as working: the backoff starts over.
const STABLE_AFTER: Duration = Duration::from_secs(3);

#[derive(Debug, Clone)]
pub struct SeatParams {
    /// The gateway's `/v1/seat` URL.
    pub url: String,
    pub session_id: Uuid,
    pub seat_id: Uuid,
    pub client_version: String,
}

#[derive(Debug)]
enum SeatCommandMsg {
    Note { command_id: Uuid, text: String },
    Leave,
}

#[derive(Clone)]
pub struct SeatHandle {
    tx: mpsc::UnboundedSender<SeatCommandMsg>,
    pub session_id: Uuid,
    pub seat_id: Uuid,
}

impl SeatHandle {
    /// Send a note for the host; returns its command id (the result names it).
    pub fn note(&self, text: String) -> Uuid {
        let command_id = Uuid::now_v7();
        let _ = self.tx.send(SeatCommandMsg::Note { command_id, text });
        command_id
    }

    /// Leave the call: the seat stays the guest's until the host removes it or the call ends.
    pub fn leave(&self) {
        let _ = self.tx.send(SeatCommandMsg::Leave);
    }
}

/// A host message, as opposed to the seat's own: only these advance the position.
pub fn is_host_message(m: &ServerMessage) -> bool {
    matches!(
        m,
        ServerMessage::TranscriptPartial(_)
            | ServerMessage::TranscriptFinal(_)
            | ServerMessage::QuestionDetected(_)
            | ServerMessage::QuestionCancelled(_)
            | ServerMessage::AnswerDelta(_)
            | ServerMessage::AnswerDone(_)
            | ServerMessage::AnswerError(_)
            | ServerMessage::Status(_)
            | ServerMessage::SessionLanguage(_)
    )
}

/// The position after a message, or None when it is a repeat to drop.
pub fn advance(last_seq: u64, m: &ServerMessage) -> Option<u64> {
    match m {
        ServerMessage::SeatSnapshot(s) => Some(s.seq),
        _ if is_host_message(m) => (m.seq() > last_seq).then(|| m.seq()),
        _ => Some(last_seq),
    }
}

enum Outcome {
    Closed { code: Option<u16> },
    Left,
}

struct Seat {
    params: SeatParams,
    tokens: Arc<dyn TokenProvider>,
    sink: EventSink,
    rx: mpsc::UnboundedReceiver<SeatCommandMsg>,
    last_seq: u64,
    joined: bool,
}

pub fn spawn(
    params: SeatParams,
    tokens: Arc<dyn TokenProvider>,
    sink: EventSink,
) -> (SeatHandle, tokio::task::JoinHandle<StopReason>) {
    let (tx, rx) = mpsc::unbounded_channel();
    let handle = SeatHandle { tx, session_id: params.session_id, seat_id: params.seat_id };
    let seat = Seat { params, tokens, sink, rx, last_seq: 0, joined: false };
    (handle, tokio::spawn(seat.run()))
}

impl Seat {
    fn emit(&self, event: UiEvent) {
        (self.sink)(event);
    }

    fn state(&self, state: ConnectionState) {
        self.emit(UiEvent::Connection { state });
    }

    async fn run(mut self) -> StopReason {
        let mut backoff = Backoff::new();
        let mut rng = StdRng::from_os_rng();
        let mut refresh = false;
        let mut leaving = false;
        let mut attempt = 0usize;
        let reason = loop {
            self.state(ConnectionState::Connecting { attempt });
            let token = if refresh { self.tokens.refresh().await } else { self.tokens.current().await };
            let token = match token {
                Ok(t) => t,
                Err(e) => {
                    self.emit(UiEvent::Warning { message: format!("no token for the seat: {e}") });
                    break StopReason::Unauthorized;
                }
            };
            let refreshed_after_401 = refresh;
            let code = match connect(&self.params.url, &token).await {
                Ok(ws) => {
                    let started = tokio::time::Instant::now();
                    match self.serve(ws).await {
                        Outcome::Left => {
                            leaving = true;
                            None
                        }
                        Outcome::Closed { code } => {
                            if started.elapsed() >= STABLE_AFTER {
                                backoff.reset();
                            }
                            code
                        }
                    }
                }
                Err(e) => {
                    self.emit(UiEvent::Warning { message: format!("seat connection failed: {e}") });
                    None
                }
            };
            if let Some(code) = code {
                self.emit(UiEvent::Closed { code: Some(code), reason: String::new() });
            }
            match close::on_seat_close(code, leaving, refreshed_after_401) {
                CloseAction::Stop(reason) => break reason,
                CloseAction::Reconnect { delay, refresh_token, .. } => {
                    refresh = refresh_token;
                    let wait = match delay {
                        Delay::Now => Duration::ZERO,
                        Delay::After(d) => d,
                        Delay::Backoff => backoff.next_delay(&mut rng),
                    };
                    attempt += 1;
                    self.state(ConnectionState::Reconnecting {
                        attempt,
                        in_ms: u64::try_from(wait.as_millis()).unwrap_or(u64::MAX),
                        close_code: code,
                    });
                    tokio::select! {
                        _ = tokio::time::sleep(wait) => {}
                        cmd = self.rx.recv() => {
                            if matches!(cmd, Some(SeatCommandMsg::Leave) | None) {
                                break StopReason::Left;
                            }
                        }
                    }
                }
            }
        };
        self.state(ConnectionState::Stopped { reason });
        self.emit(UiEvent::Stopped { reason });
        reason
    }

    async fn send(ws: &mut Ws, message: ClientMessage) -> Result<(), String> {
        let text = message.to_json().map_err(|e| e.to_string())?;
        ws.send(Message::text(text)).await.map_err(|e| e.to_string())
    }

    async fn serve(&mut self, mut ws: Ws) -> Outcome {
        let join = ClientMessage::SeatJoin(SeatJoin {
            session_id: self.params.session_id,
            seat_id: self.params.seat_id,
            last_seq: self.last_seq,
            client_version: self.params.client_version.clone(),
        });
        if let Err(e) = Self::send(&mut ws, join).await {
            self.emit(UiEvent::Warning { message: format!("seat.join not sent: {e}") });
            return Outcome::Closed { code: None };
        }
        let mut ping = tokio::time::interval(PING_INTERVAL);
        ping.tick().await;
        loop {
            tokio::select! {
                incoming = ws.next() => match incoming {
                    Some(Ok(Message::Text(text))) => {
                        let message = match parse_server(&text) {
                            Ok(m) => m,
                            Err(e) => {
                                self.emit(UiEvent::Warning { message: format!("ignored a seat message: {e}") });
                                continue;
                            }
                        };
                        let Some(next) = advance(self.last_seq, &message) else { continue };
                        self.last_seq = next;
                        if let ServerMessage::SeatReady(ready) = &message {
                            let resumed = self.joined;
                            self.joined = true;
                            self.state(ConnectionState::Live { session_id: ready.session_id, resumed });
                        }
                        self.emit(UiEvent::Server { message });
                    }
                    Some(Ok(Message::Close(frame))) => {
                        let code = frame.map(|f| u16::from(f.code));
                        let _ = tokio::time::timeout(Duration::from_millis(500), ws.close(None)).await;
                        return Outcome::Closed { code };
                    }
                    Some(Ok(_)) => {}
                    Some(Err(_)) | None => return Outcome::Closed { code: None },
                },
                cmd = self.rx.recv() => match cmd {
                    Some(SeatCommandMsg::Note { command_id, text }) => {
                        let m = ClientMessage::SeatCommand(SeatCommand { command_id, kind: SeatCommandKind::Note, text });
                        if let Err(e) = Self::send(&mut ws, m).await {
                            self.emit(UiEvent::Warning { message: format!("note not sent: {e}") });
                        }
                    }
                    Some(SeatCommandMsg::Leave) | None => {
                        let frame = CloseFrame { code: WsCloseCode::Normal, reason: "left".into() };
                        let _ = tokio::time::timeout(Duration::from_secs(2), ws.close(Some(frame))).await;
                        return Outcome::Left;
                    }
                },
                _ = ping.tick() => {
                    if ws.send(Message::Ping(Vec::new().into())).await.is_err() {
                        return Outcome::Closed { code: None };
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use copilot_protocol::parse_server;

    use super::*;

    fn msg(json: &str) -> ServerMessage {
        parse_server(json).unwrap()
    }

    #[test]
    fn host_messages_advance_and_repeats_are_dropped() {
        let delta = msg(
            r#"{"type":"answer.delta","seq":40,"question_id":"0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210","pass":"fast","text":"x"}"#,
        );
        assert_eq!(advance(39, &delta), Some(40));
        assert_eq!(advance(40, &delta), None);
        assert_eq!(advance(41, &delta), None);
    }

    #[test]
    fn the_seats_own_messages_pass_and_a_snapshot_sets_the_position() {
        let host = msg(r#"{"type":"seat.host","seq":12,"connected":false,"sharing":true}"#);
        assert_eq!(advance(40, &host), Some(40));
        let snap = msg(r#"{"type":"seat.snapshot","seq":12,"messages":[]}"#);
        assert_eq!(advance(40, &snap), Some(12));
        assert!(!is_host_message(&host) && !is_host_message(&snap));
    }
}
