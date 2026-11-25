//! Loopback regressions: no OAuth endpoint or provider calls.
use copilot_client::audio::OutFrame;
use copilot_client::auth::{AuthError, BoxFuture, Token, TokenProvider};
use copilot_client::net::client::{
    self, Adopt, ClientConfig, ClientEvent, Command, ConnectionState, StartParams,
};
use copilot_client::net::close::StopReason;
use copilot_protocol::audio::{CHANNEL_THEM, FLAG_SPEECH_START};
use copilot_protocol::{Mode, ServerMessage};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::{Duration, SystemTime};
use tokio::net::TcpListener;
use tokio::sync::{Semaphore, mpsc};
use tokio_tungstenite::tungstenite::{Message, protocol::CloseFrame};
use uuid::Uuid;

const WAIT: Duration = Duration::from_secs(3);
struct Owned<T>(tokio::task::JoinHandle<T>);
impl<T> Owned<T> {
    async fn finish(mut self) -> T {
        match tokio::time::timeout(WAIT, &mut self.0).await {
            Ok(value) => value.expect("authored task panicked"),
            Err(_) => {
                self.0.abort();
                let _ = tokio::time::timeout(WAIT, &mut self.0).await;
                panic!("authored task exceeded deadline");
            }
        }
    }
    async fn abort(mut self) {
        self.0.abort();
        let _ = tokio::time::timeout(WAIT, &mut self.0).await;
    }
}
impl<T> Drop for Owned<T> {
    fn drop(&mut self) {
        self.0.abort();
    }
}

struct GatedTokens {
    starts: Semaphore,
    releases: Semaphore,
    calls: AtomicUsize,
    current_calls: AtomicUsize,
    active: AtomicUsize,
    max_active: AtomicUsize,
    cancelled: AtomicUsize,
    committed: AtomicBool,
    fail_first: bool,
    near_expiry: bool,
}
impl GatedTokens {
    fn new(fail_first: bool, near_expiry: bool) -> Arc<Self> {
        Arc::new(Self {
            starts: Semaphore::new(0),
            releases: Semaphore::new(0),
            calls: AtomicUsize::new(0),
            current_calls: AtomicUsize::new(0),
            active: AtomicUsize::new(0),
            max_active: AtomicUsize::new(0),
            cancelled: AtomicUsize::new(0),
            committed: AtomicBool::new(false),
            fail_first,
            near_expiry,
        })
    }
    fn token(&self) -> Token {
        let committed = self.committed.load(Ordering::SeqCst);
        Token {
            access_token: if committed { "local-rotated" } else { "local-initial" }.into(),
            expires_at: if committed {
                Some(SystemTime::now() + Duration::from_secs(3600))
            } else if self.near_expiry {
                Some(SystemTime::now() + Duration::from_secs(100))
            } else {
                None
            },
        }
    }
    async fn started(&self) {
        tokio::time::timeout(WAIT, self.starts.acquire()).await.unwrap().unwrap().forget();
    }
}
struct RefreshScope<'a> {
    tokens: &'a GatedTokens,
    settled: bool,
}
impl Drop for RefreshScope<'_> {
    fn drop(&mut self) {
        self.tokens.active.fetch_sub(1, Ordering::SeqCst);
        if !self.settled {
            self.tokens.cancelled.fetch_add(1, Ordering::SeqCst);
        }
    }
}
impl TokenProvider for GatedTokens {
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move {
            self.current_calls.fetch_add(1, Ordering::SeqCst);
            assert_eq!(self.active.load(Ordering::SeqCst), 0, "current raced accepted rotation");
            Ok(self.token())
        })
    }
    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async move {
            let call = self.calls.fetch_add(1, Ordering::SeqCst);
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.max_active.fetch_max(active, Ordering::SeqCst);
            let mut scope = RefreshScope { tokens: self, settled: false };
            self.starts.add_permits(1);
            self.releases.acquire().await.unwrap().forget();
            scope.settled = true;
            if self.fail_first && call == 0 {
                return Err(AuthError::Store("authored refresh failure".into()));
            }
            self.committed.store(true, Ordering::SeqCst);
            Ok(self.token())
        })
    }
}

enum Wire {
    Send(Value),
    Close(u16),
}
struct Peer {
    commands: mpsc::UnboundedSender<Wire>,
    incoming: mpsc::UnboundedReceiver<(usize, Message)>,
    closed: mpsc::UnboundedReceiver<usize>,
    task: Owned<()>,
}
impl Peer {
    fn send(&self, value: Value) {
        self.commands.send(Wire::Send(value)).unwrap();
    }
    fn close(&self, code: u16) {
        self.commands.send(Wire::Close(code)).unwrap();
    }
    async fn closed(&mut self, expected: usize) {
        assert_eq!(tokio::time::timeout(WAIT, self.closed.recv()).await.unwrap(), Some(expected));
    }
    async fn next(&mut self, predicate: impl Fn(usize, &Message) -> bool) -> (usize, Message) {
        tokio::time::timeout(WAIT, async {
            loop {
                let row = self.incoming.recv().await.expect("peer exited");
                if predicate(row.0, &row.1) {
                    return row;
                }
            }
        })
        .await
        .unwrap()
    }
}
fn has_type(message: &Message, kind: &str) -> bool {
    message
        .to_text()
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .is_some_and(|v| v["type"] == kind)
}
async fn event(events: &mut mpsc::UnboundedReceiver<ClientEvent>, predicate: impl Fn(&ClientEvent) -> bool) {
    tokio::time::timeout(WAIT, async {
        loop {
            let e = events.recv().await.expect("client exited");
            if predicate(&e) {
                return;
            }
        }
    })
    .await
    .unwrap();
}
fn answer() -> Value {
    json!({"type":"answer.delta", "question_id":Uuid::now_v7(), "pass":"fast", "text":"A useful complete answer.", "sequence":0})
}
fn expiry() -> Value {
    json!({"type":"auth.expiring", "expires_in_s":120})
}
async fn open(
    tokens: Arc<GatedTokens>,
) -> (client::ClientHandle, Owned<StopReason>, mpsc::UnboundedReceiver<ClientEvent>, Peer) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let (commands, mut control) = mpsc::unbounded_channel();
    let (wire, incoming) = mpsc::unbounded_channel();
    let (closed_tx, closed) = mpsc::unbounded_channel();
    let task = Owned(tokio::spawn(async move {
        let mut seq = 0_u64;
        for connection in 1..=2 {
            let (stream, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
            let first = ws.next().await.unwrap().unwrap();
            assert!(has_type(&first, "session.resume"));
            wire.send((connection, first)).unwrap();
            seq += 1;
            ws.send(Message::text(
                json!({"type":"status","seq":seq,"component":"stt","state":"ok","message":"Ready"})
                    .to_string(),
            ))
            .await
            .unwrap();
            loop {
                tokio::select! {
                    command = control.recv() => match command {
                        Some(Wire::Send(mut value)) => {
                            seq += 1; value["seq"] = json!(seq);
                            ws.send(Message::text(value.to_string())).await.unwrap();
                        }
                        Some(Wire::Close(code)) => {
                            ws.send(Message::Close(Some(CloseFrame { code: code.into(), reason: "authored close".into() }))).await.unwrap();
                            tokio::time::timeout(WAIT, async {
                                while let Some(Ok(message)) = ws.next().await {
                                    let closed = message.is_close();
                                    wire.send((connection, message)).unwrap();
                                    if closed { break; }
                                }
                            }).await.expect("client socket did not close");
                            break;
                        }
                        None => return,
                    },
                    incoming = ws.next() => match incoming {
                        Some(Ok(message)) => {
                            let closed = message.is_close();
                            wire.send((connection, message)).unwrap();
                            if closed { let _ = ws.flush().await; break; }
                        }
                        _ => break,
                    },
                }
            }
            closed_tx.send(connection).unwrap();
        }
    }));
    let params = StartParams {
        spoken_language: None,
        mode: Mode::Interview,
        job_id: None,
        interview_id: None,
        meeting_title: None,
        employer_id: None,
        agenda: None,
        material_ids: vec![],
        platform: "local-test".into(),
        client_version: "test".into(),
        store_transcript: true,
        answer_style: None,
        response_length: None,
        no_call: false,
    };
    let mut config = ClientConfig::new(url, params);
    config.adopt =
        Some(Adopt { session_id: Uuid::now_v7(), next_frame: [0, 0], me_audio: false, capture_offset_ms: 0 });
    let (tx, mut events) = mpsc::unbounded_channel();
    let (handle, client) = client::spawn(config, tokens, tx);
    let client = Owned(client);
    event(&mut events, |e| matches!(e, ClientEvent::Connection(ConnectionState::Live { .. }))).await;
    (handle, client, events, Peer { commands, incoming, closed, task })
}

#[tokio::test]
async fn slow_refresh_keeps_answer_audio_and_end_command_responsive_and_settles_rotation() {
    let tokens = GatedTokens::new(false, false);
    let (handle, client, mut events, mut peer) = open(tokens.clone()).await;
    peer.send(expiry());
    tokens.started().await;
    peer.send(answer());
    event(&mut events, |e| matches!(e, ClientEvent::Server { message: ServerMessage::AnswerDelta(_) })).await;
    handle.send_frame(OutFrame {
        channel: CHANNEL_THEM,
        flags: FLAG_SPEECH_START,
        capture_ms: 100,
        audio: vec![1; 320],
    });
    peer.next(|_, m| m.is_binary()).await;
    handle.command(Command::End { summarize: false }).await;
    peer.next(|_, m| has_type(m, "session.end")).await;
    peer.close(1000);
    peer.closed(1).await;
    // Stop was sent and acknowledged, but accepted rotation is not abandoned.
    assert!(!client.0.is_finished());
    tokens.releases.add_permits(1);
    assert_eq!(client.finish().await, StopReason::Ended);
    assert!(tokens.committed.load(Ordering::SeqCst));
    assert_eq!(tokens.cancelled.load(Ordering::SeqCst), 0);
    assert_eq!(tokens.active.load(Ordering::SeqCst), 0);
    peer.task.abort().await;
}

#[tokio::test]
async fn repeated_expiry_notice_has_one_inflight_refresh_and_one_auth_write() {
    let tokens = GatedTokens::new(false, false);
    let (handle, client, mut events, mut peer) = open(tokens.clone()).await;
    peer.send(expiry());
    tokens.started().await;
    peer.send(expiry());
    peer.send(answer());
    event(&mut events, |e| matches!(e, ClientEvent::Server { message: ServerMessage::AnswerDelta(_) })).await;
    assert_eq!(tokens.calls.load(Ordering::SeqCst), 1);
    tokens.releases.add_permits(1);
    peer.next(|_, m| has_type(m, "auth.refresh")).await;
    assert_eq!(tokens.max_active.load(Ordering::SeqCst), 1);
    assert_eq!(handle.stats().token_refreshes, 1);
    handle.command(Command::Shutdown).await;
    assert_eq!(client.finish().await, StopReason::Ended);
    assert_eq!(tokens.cancelled.load(Ordering::SeqCst), 0);
    peer.task.abort().await;
}

#[tokio::test]
async fn expiry_timer_failure_preserves_warning_retry_and_live_answers() {
    let tokens = GatedTokens::new(true, true);
    let (handle, client, mut events, mut peer) = open(tokens.clone()).await;
    tokens.started().await;
    tokens.releases.add_permits(1);
    event(
        &mut events,
        |e| matches!(e, ClientEvent::Warning { message } if message.contains("authored refresh failure")),
    )
    .await;
    // Existing expiry policy retries while the old expiry is due; no new backoff is invented.
    tokens.started().await;
    peer.send(answer());
    event(&mut events, |e| matches!(e, ClientEvent::Server { message: ServerMessage::AnswerDelta(_) })).await;
    tokens.releases.add_permits(1);
    peer.next(|_, m| has_type(m, "auth.refresh")).await;
    assert_eq!(tokens.calls.load(Ordering::SeqCst), 2);
    assert_eq!(tokens.max_active.load(Ordering::SeqCst), 1);
    assert_eq!(handle.stats().token_refreshes, 1);
    handle.command(Command::Shutdown).await;
    assert_eq!(client.finish().await, StopReason::Ended);
    peer.task.abort().await;
}

#[tokio::test]
async fn reconnect_settles_owned_rotation_before_reading_current_credentials() {
    let tokens = GatedTokens::new(false, false);
    let (handle, client, mut events, mut peer) = open(tokens.clone()).await;
    peer.send(expiry());
    tokens.started().await;
    peer.close(1012);
    peer.closed(1).await;
    assert_eq!(tokens.current_calls.load(Ordering::SeqCst), 1);
    tokens.releases.add_permits(1);
    peer.next(|n, m| n == 2 && has_type(m, "session.resume")).await;
    event(&mut events, |e| matches!(e, ClientEvent::Connection(ConnectionState::Live { resumed: true, .. })))
        .await;
    assert_eq!(tokens.current_calls.load(Ordering::SeqCst), 2);
    assert_eq!(tokens.calls.load(Ordering::SeqCst), 1);
    assert!(tokens.committed.load(Ordering::SeqCst));
    assert_eq!(tokens.cancelled.load(Ordering::SeqCst), 0);
    handle.command(Command::Shutdown).await;
    assert_eq!(client.finish().await, StopReason::Ended);
    peer.task.abort().await;
}

#[tokio::test]
async fn shutdown_and_cancelled_completion_waiter_preserve_accepted_rotation() {
    let tokens = GatedTokens::new(false, false);
    let (handle, mut client, _events, mut peer) = open(tokens.clone()).await;
    peer.send(expiry());
    tokens.started().await;
    handle.command(Command::Shutdown).await;
    peer.next(|_, message| message.is_close()).await;
    // Dropping an end/quit waiter must not abandon the independently owned client.
    assert!(tokio::time::timeout(Duration::from_millis(10), &mut client.0).await.is_err());
    assert_eq!(tokens.active.load(Ordering::SeqCst), 1);
    assert_eq!(tokens.cancelled.load(Ordering::SeqCst), 0);
    assert_eq!(tokens.current_calls.load(Ordering::SeqCst), 1);
    tokens.releases.add_permits(1);
    assert_eq!(client.finish().await, StopReason::Ended);
    assert!(tokens.committed.load(Ordering::SeqCst));
    assert_eq!(tokens.cancelled.load(Ordering::SeqCst), 0);
    assert_eq!(tokens.active.load(Ordering::SeqCst), 0);
    peer.task.abort().await;
}
