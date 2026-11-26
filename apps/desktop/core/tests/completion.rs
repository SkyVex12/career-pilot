use copilot_client::auth::{AuthError, Token, TokenProvider};
use copilot_client::net::client::{
    self, Adopt, ClientConfig, ClientEvent, Command, ConnectionState, StartParams,
};
use copilot_client::net::close::StopReason;
use copilot_protocol::{Mode, ServerMessage};
use futures_util::{SinkExt, StreamExt, future::BoxFuture};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::{Message, protocol::CloseFrame};
use uuid::Uuid;
/// Keep every authored socket/client task owned even when a test assertion or timeout fails.
struct OwnedTask<T>(tokio::task::JoinHandle<T>);
impl<T> OwnedTask<T> {
    fn new(task: tokio::task::JoinHandle<T>) -> Self {
        Self(task)
    }
    async fn finish(mut self, timeout: Duration) -> T {
        match tokio::time::timeout(timeout, &mut self.0).await {
            Ok(result) => result.expect("authored task panicked"),
            Err(_) => {
                self.0.abort();
                let _ = tokio::time::timeout(Duration::from_secs(1), &mut self.0).await;
                panic!("authored task exceeded its deadline");
            }
        }
    }
}
impl<T> Drop for OwnedTask<T> {
    fn drop(&mut self) {
        self.0.abort();
    }
}

struct Tokens;
impl TokenProvider for Tokens {
    fn current(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        Box::pin(async { Ok(Token { access_token: "local-test".into(), expires_at: None }) })
    }
    fn refresh(&self) -> BoxFuture<'_, Result<Token, AuthError>> {
        self.current()
    }
}
fn params() -> StartParams {
    StartParams {
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
        no_call: true,
    }
}
async fn scenario(
    stop: bool,
    error: Option<(&'static str, bool)>,
    close: Option<u16>,
    wait_for_timeout: bool,
) -> (StopReason, Vec<ClientEvent>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let (sent, received) = oneshot::channel();
    let (release, closed) = oneshot::channel::<()>();
    let server = OwnedTask::new(tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        assert!(ws.next().await.unwrap().unwrap().to_text().unwrap().contains("session.resume"));
        ws.send(Message::text(
            r#"{"type":"status","seq":1,"component":"stt","state":"ok","message":"Ready"}"#,
        ))
        .await
        .unwrap();
        if stop {
            loop {
                let msg = ws.next().await.unwrap().unwrap();
                if msg.is_text() && msg.to_text().unwrap().contains("session.end") {
                    break;
                }
            }
        }
        if let Some((code, fatal)) = error {
            ws.send(Message::text(serde_json::json!({"type":"error","seq":2,"code":code,"message":"Authored write failure","fatal":fatal}).to_string())).await.unwrap();
        }
        let _ = sent.send(());
        if wait_for_timeout {
            let _ = closed.await;
            return;
        }
        if let Some(code) = close {
            ws.send(Message::Close(Some(CloseFrame { code: code.into(), reason: "authored close".into() })))
                .await
                .unwrap();
            let _ = tokio::time::timeout(Duration::from_secs(1), ws.next()).await;
        }
    }));
    let sid = Uuid::now_v7();
    let mut config = ClientConfig::new(url, params());
    config.adopt = Some(Adopt { session_id: sid, next_frame: [0, 0], me_audio: false, capture_offset_ms: 0 });
    let (tx, mut events) = mpsc::unbounded_channel();
    let (handle, task) = client::spawn(config, Arc::new(Tokens), tx);
    let task = OwnedTask::new(task);
    let mut seen = vec![];
    loop {
        let incoming = tokio::time::timeout(Duration::from_secs(3), events.recv()).await.unwrap().unwrap();
        let live = matches!(&incoming, ClientEvent::Connection(ConnectionState::Live { .. }));
        seen.push(incoming);
        if live {
            break;
        }
    }
    if stop {
        handle.command(Command::End { summarize: false }).await;
    }
    tokio::time::timeout(Duration::from_secs(3), received).await.unwrap().unwrap();
    let reason = task.finish(Duration::from_secs(12)).await;
    let _ = release.send(());
    server.finish(Duration::from_secs(3)).await;
    while let Ok(event) = events.try_recv() {
        seen.push(event);
    }
    (reason, seen)
}
fn check_order(events: &[ClientEvent]) {
    let error = events
        .iter()
        .position(|event| matches!(event, ClientEvent::Server { message: ServerMessage::ServerError(_) }))
        .unwrap();
    let closed = events.iter().position(|event| matches!(event, ClientEvent::Closed { .. })).unwrap();
    let stopped = events
        .iter()
        .position(|event| {
            matches!(
                event,
                ClientEvent::Connection(ConnectionState::Stopped { reason: StopReason::RetentionFailed })
            )
        })
        .unwrap();
    assert!(error < closed && closed < stopped);
    assert!(
        !events
            .iter()
            .any(|event| matches!(event, ClientEvent::Connection(ConnectionState::Reconnecting { .. })))
    );
}
#[tokio::test]
async fn fatal_retention_before_automatic_close_is_not_a_reconnect_or_success() {
    let (reason, events) = scenario(false, Some(("retention_unavailable", true)), Some(4500), false).await;
    assert_eq!(reason, StopReason::RetentionFailed);
    check_order(&events);
}
#[tokio::test]
async fn fatal_retention_after_stop_does_not_become_success() {
    let (reason, events) = scenario(true, Some(("retention_unavailable", true)), Some(4500), false).await;
    assert_eq!(reason, StopReason::RetentionFailed);
    check_order(&events);
}
#[tokio::test]
async fn explicit_failure_survives_a_later_normal_close() {
    let (reason, events) = scenario(true, Some(("retention_unavailable", true)), Some(1000), false).await;
    assert_eq!(reason, StopReason::RetentionFailed);
    check_order(&events);
}
#[tokio::test]
async fn stopped_network_drop_is_unknown_not_failed_or_saved() {
    assert_eq!(scenario(true, None, None, false).await.0, StopReason::EndUnconfirmed);
}
#[tokio::test]
async fn stopped_internal_close_is_unknown_without_retention_evidence() {
    assert_eq!(scenario(true, None, Some(4500), false).await.0, StopReason::EndUnconfirmed);
}
#[tokio::test]
async fn normal_gateway_completion_stays_success() {
    assert_eq!(scenario(true, None, Some(1000), false).await.0, StopReason::Ended);
}
#[tokio::test]
async fn unrelated_or_nonfatal_errors_do_not_claim_saving_failed() {
    for error in [("other", true), ("retention_unavailable", false)] {
        assert_eq!(scenario(true, Some(error), Some(1000), false).await.0, StopReason::Ended);
    }
}
#[tokio::test]
async fn stop_timeout_is_unconfirmed() {
    assert_eq!(scenario(true, None, None, true).await.0, StopReason::EndUnconfirmed);
}

#[tokio::test]
async fn stop_while_offline_does_not_claim_server_completion() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let server = OwnedTask::new(tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        let _ = ws.next().await;
        ws.send(Message::text(
            r#"{"type":"status","seq":1,"component":"stt","state":"ok","message":"Ready"}"#,
        ))
        .await
        .unwrap();
    }));
    let mut config = ClientConfig::new(url, params());
    config.adopt =
        Some(Adopt { session_id: Uuid::now_v7(), next_frame: [0, 0], me_audio: false, capture_offset_ms: 0 });
    let (tx, mut events) = mpsc::unbounded_channel();
    let (handle, task) = client::spawn(config, Arc::new(Tokens), tx);
    let task = OwnedTask::new(task);
    loop {
        let event = tokio::time::timeout(Duration::from_secs(3), events.recv()).await.unwrap().unwrap();
        if matches!(event, ClientEvent::Connection(ConnectionState::Reconnecting { .. })) {
            break;
        }
    }
    handle.command(Command::End { summarize: false }).await;
    assert_eq!(task.finish(Duration::from_secs(3)).await, StopReason::EndUnconfirmed);
    server.finish(Duration::from_secs(3)).await;
}

async fn end_other(error: Option<(&'static str, bool)>, close: Option<u16>) -> Result<Option<u16>, String> {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let server = OwnedTask::new(tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        assert!(ws.next().await.unwrap().unwrap().to_text().unwrap().contains("session.resume"));
        assert!(ws.next().await.unwrap().unwrap().to_text().unwrap().contains("session.end"));
        if let Some((code, fatal)) = error {
            ws.send(Message::text(serde_json::json!({"type":"error","seq":1,"code":code,"fatal":fatal,"message":"Authored write failure"}).to_string())).await.unwrap();
        }
        if let Some(code) = close {
            ws.send(Message::Close(Some(CloseFrame { code: code.into(), reason: "authored close".into() })))
                .await
                .unwrap();
            let _ = tokio::time::timeout(Duration::from_millis(500), ws.next()).await;
        }
    }));
    let result =
        tokio::time::timeout(Duration::from_secs(3), client::end_session(&url, &Tokens, Uuid::now_v7()))
            .await
            .unwrap();
    server.finish(Duration::from_secs(3)).await;
    result
}
#[tokio::test]
async fn detached_end_keeps_existing_confirmed_codes() {
    for code in [1000, 4408, 4403] {
        assert_eq!(end_other(None, Some(code)).await, Ok(Some(code)));
    }
}
#[tokio::test]
async fn detached_end_rejects_unconfirmed_close_and_transport_loss() {
    for code in [None, Some(4500), Some(1001), Some(4401), Some(4426)] {
        assert!(end_other(None, code).await.unwrap_err().contains("did not confirm"));
    }
}
#[tokio::test]
async fn detached_end_preserves_explicit_failure_before_normal_close_or_loss() {
    for code in [Some(4500), Some(1000), None] {
        assert!(
            end_other(Some(("retention_unavailable", true)), code)
                .await
                .unwrap_err()
                .contains("could not save")
        );
    }
}
#[tokio::test]
async fn detached_end_does_not_invent_failure_from_an_unrelated_or_nonfatal_error() {
    for error in [("other", true), ("retention_unavailable", false)] {
        assert_eq!(end_other(Some(error), Some(1000)).await, Ok(Some(1000)));
    }
}

#[tokio::test]
async fn abort_guard_stops_an_owned_loopback_task_on_early_exit() {
    struct Stopped(Option<oneshot::Sender<()>>);
    impl Drop for Stopped {
        fn drop(&mut self) {
            if let Some(signal) = self.0.take() {
                let _ = signal.send(());
            }
        }
    }
    let (started, start) = oneshot::channel();
    let (stopped, stop) = oneshot::channel();
    let task = OwnedTask::new(tokio::spawn(async move {
        let _signal = Stopped(Some(stopped));
        let _ = started.send(());
        std::future::pending::<()>().await;
    }));
    start.await.unwrap();
    drop(task);
    tokio::time::timeout(Duration::from_secs(1), stop).await.unwrap().unwrap();
}

#[tokio::test]
async fn timeout_after_automatic_restart_retains_old_close_history_but_is_unconfirmed() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let sid = Uuid::now_v7();
    let (release, closed) = oneshot::channel();
    let server = OwnedTask::new(tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut first = tokio_tungstenite::accept_async(stream).await.unwrap();
        assert!(first.next().await.unwrap().unwrap().to_text().unwrap().contains("session.start"));
        first
            .send(Message::Close(Some(CloseFrame { code: 4408.into(), reason: "restart".into() })))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_millis(500), first.next()).await;
        let (stream, _) = listener.accept().await.unwrap();
        let mut current = tokio_tungstenite::accept_async(stream).await.unwrap();
        assert!(current.next().await.unwrap().unwrap().to_text().unwrap().contains("session.start"));
        current.send(Message::text(serde_json::json!({"type":"session.ready","seq":1,"session_id":sid,"server_time":"2026-11-25T12:00:00Z","limits":{"max_minutes":60}}).to_string())).await.unwrap();
        loop {
            let message = current.next().await.unwrap().unwrap();
            if message.is_text() && message.to_text().unwrap().contains("session.end") {
                break;
            }
        }
        let _ = closed.await;
    }));
    let (tx, mut events) = mpsc::unbounded_channel();
    let (handle, task) = client::spawn(ClientConfig::new(url, params()), Arc::new(Tokens), tx);
    let task = OwnedTask::new(task);
    loop {
        let event = tokio::time::timeout(Duration::from_secs(3), events.recv()).await.unwrap().unwrap();
        if matches!(event, ClientEvent::Connection(ConnectionState::Live { .. })) {
            break;
        }
    }
    handle.command(Command::End { summarize: false }).await;
    assert_eq!(task.finish(Duration::from_secs(12)).await, StopReason::EndUnconfirmed);
    let stats = handle.stats();
    assert_eq!(stats.close_codes, vec![Some(4408)]);
    assert_eq!(stats.session_ids, vec![sid]);
    let _ = release.send(());
    server.finish(Duration::from_secs(3)).await;
}

#[tokio::test]
async fn failed_adoption_cannot_mask_explicit_retention_error_as_already_ended() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let server = OwnedTask::new(tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        assert!(ws.next().await.unwrap().unwrap().to_text().unwrap().contains("session.resume"));
        ws.send(Message::text(serde_json::json!({"type":"error","seq":1,"code":"retention_unavailable","message":"Authored write failure","fatal":true}).to_string())).await.unwrap();
        ws.send(Message::Close(Some(CloseFrame { code: 4408.into(), reason: "adoption failed".into() })))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_millis(500), ws.next()).await;
    }));
    let mut config = ClientConfig::new(url, params());
    config.adopt =
        Some(Adopt { session_id: Uuid::now_v7(), next_frame: [0, 0], me_audio: false, capture_offset_ms: 0 });
    let (tx, mut events) = mpsc::unbounded_channel();
    let (_handle, task) = client::spawn(config, Arc::new(Tokens), tx);
    assert_eq!(OwnedTask::new(task).finish(Duration::from_secs(3)).await, StopReason::RetentionFailed);
    server.finish(Duration::from_secs(3)).await;
    let mut seen = vec![];
    while let Ok(event) = events.try_recv() {
        seen.push(event);
    }
    check_order(&seen);
    assert!(!seen.iter().any(|event| matches!(event, ClientEvent::Connection(ConnectionState::Live { .. }))));
}
