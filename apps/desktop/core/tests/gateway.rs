//! The client against the real gateway: `python -m copilot_gateway.offline`, the gateway's own
//! code with scripted STT and Claude (no keys, no network). The fake audio source speaks the
//! way the offline STT listens: words as UTF-8 in pcm16-sized frames, with the speech flags
//! the VAD would set.
//!
//! Needs the Python workspace (`uv sync`). The restart test also needs Valkey on
//! localhost:6379 (`docker compose up -d`); without it, that test says so and passes.

mod support;

use std::sync::Arc;
use std::time::Duration;

use copilot_client::audio::OutFrame;
use copilot_client::auth::{StaticTokens, Token};
use copilot_client::net::client::{
    self, ClientConfig, ClientEvent, ClientHandle, Command, ConnectionState, StartParams,
};
use copilot_client::net::close::StopReason;
use copilot_client::protocol::audio::{CHANNEL_ME, CHANNEL_THEM, FLAG_SPEECH_END, FLAG_SPEECH_START};
use copilot_client::protocol::{ClientMessage, Mode, Pass, QuestionForce, ServerMessage};
use support::{Gateway, TOKEN, free_port};
use tokio::sync::mpsc::UnboundedReceiver;
use uuid::Uuid;

fn start_params() -> StartParams {
    StartParams {
        mode: Mode::Interview,
        job_id: None,
        meeting_title: None,
        platform: "zoom".into(),
        client_version: "test".into(),
        store_transcript: true,
    }
}

/// Words as the offline STT hears them: UTF-8, padded with zeros to whole 100 ms frames
/// (`copilot_gateway.testing.text_frame_audio`), as pcm16 samples.
fn text_audio(text: &str) -> Vec<i16> {
    let mut bytes = format!(" {text}").into_bytes();
    let size = bytes.len().div_ceil(3200).max(1) * 3200;
    bytes.resize(size, 0);
    bytes.as_chunks::<2>().0.iter().map(|b| i16::from_le_bytes(*b)).collect()
}

struct Call {
    handle: ClientHandle,
    events: UnboundedReceiver<ClientEvent>,
    task: tokio::task::JoinHandle<StopReason>,
    seen: Vec<ClientEvent>,
    capture_ms: u32,
}

impl Call {
    fn start(url: &str, tokens: Arc<StaticTokens>, ping: Option<Duration>) -> Self {
        let mut config = ClientConfig::new(url, start_params());
        if let Some(p) = ping {
            config.ping_interval = p;
        }
        let (tx, events) = tokio::sync::mpsc::unbounded_channel();
        let (handle, task) = client::spawn(config, tokens, tx);
        Self { handle, events, task, seen: Vec::new(), capture_ms: 0 }
    }

    /// The other side says `text`: a start frame with the words, then the empty end frame.
    fn say_start(&mut self, text: &str) {
        let audio = text_audio(text);
        self.capture_ms += 100;
        let ms = (audio.len() / 16) as u32;
        self.handle.send_frame(OutFrame {
            channel: CHANNEL_THEM,
            flags: FLAG_SPEECH_START,
            capture_ms: self.capture_ms,
            audio,
        });
        self.capture_ms += ms;
    }

    fn say_end(&mut self) {
        self.handle.send_frame(OutFrame {
            channel: CHANNEL_THEM,
            flags: FLAG_SPEECH_END,
            capture_ms: self.capture_ms,
            audio: vec![],
        });
    }

    fn say(&mut self, text: &str) {
        self.say_start(text);
        self.say_end();
    }

    fn me_speaks(&mut self) {
        for flags in [FLAG_SPEECH_START, FLAG_SPEECH_END] {
            self.capture_ms += 300;
            self.handle.send_frame(OutFrame {
                channel: CHANNEL_ME,
                flags,
                capture_ms: self.capture_ms,
                audio: vec![],
            });
        }
    }

    async fn wait<T>(&mut self, what: &str, secs: u64, mut pick: impl FnMut(&ClientEvent) -> Option<T>) -> T {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(secs);
        loop {
            match tokio::time::timeout_at(deadline, self.events.recv()).await {
                Ok(Some(e)) => {
                    let found = pick(&e);
                    self.seen.push(e);
                    if let Some(v) = found {
                        return v;
                    }
                }
                Ok(None) => panic!("client stopped while waiting for {what}; saw {}", self.summary()),
                Err(_) => panic!("timed out waiting for {what}; saw {}", self.summary()),
            }
        }
    }

    fn summary(&self) -> String {
        let items: Vec<String> = self
            .seen
            .iter()
            .map(|e| match e {
                ClientEvent::Server { message } => format!("{}#{}", message.type_name(), message.seq()),
                ClientEvent::Connection(s) => format!("{s:?}"),
                ClientEvent::Closed { code, .. } => format!("closed {code:?}"),
                ClientEvent::Warning { message } => format!("warning {message}"),
            })
            .collect();
        items.join(", ")
    }

    async fn live(&mut self) -> (Uuid, bool) {
        self.wait("live", 15, |e| match e {
            ClientEvent::Connection(ConnectionState::Live { session_id, resumed }) => {
                Some((*session_id, *resumed))
            }
            _ => None,
        })
        .await
    }

    async fn server(&mut self, what: &str, pred: impl Fn(&ServerMessage) -> bool) -> ServerMessage {
        self.wait(what, 15, |e| match e {
            ClientEvent::Server { message } if pred(message) => Some(message.clone()),
            _ => None,
        })
        .await
    }

    async fn question(&mut self) -> (Uuid, String) {
        match self.server("question.detected", |m| matches!(m, ServerMessage::QuestionDetected(_))).await {
            ServerMessage::QuestionDetected(q) => (q.question_id, q.text),
            _ => unreachable!(),
        }
    }

    async fn answer_done(&mut self, qid: Uuid, pass: Pass) {
        self.server(
            "answer.done",
            |m| matches!(m, ServerMessage::AnswerDone(d) if d.question_id == qid && d.pass == pass),
        )
        .await;
    }

    async fn stopped(self) -> (StopReason, Vec<ClientEvent>, Self) {
        let mut me = self;
        let reason =
            tokio::time::timeout(Duration::from_secs(15), &mut me.task).await.expect("client stops").unwrap();
        while let Ok(e) = me.events.try_recv() {
            me.seen.push(e);
        }
        let seen = me.seen.clone();
        (reason, seen, me)
    }

    fn server_messages(&self) -> Vec<&ServerMessage> {
        self.seen
            .iter()
            .filter_map(|e| match e {
                ClientEvent::Server { message } => Some(message),
                _ => None,
            })
            .collect()
    }
}

fn tokens(t: &str) -> Arc<StaticTokens> {
    Arc::new(StaticTokens::one(t))
}

#[tokio::test(flavor = "multi_thread")]
async fn a_call_with_questions_answers_and_a_dropped_socket() {
    let gw = Gateway::start(0, &[TOKEN], None);
    let mut call = Call::start(&gw.url(), tokens(TOKEN), Some(Duration::from_millis(300)));
    let (sid, resumed) = call.live().await;
    assert!(!resumed);

    // A spoken behavioral question: talking points, then the deep answer on its own.
    call.say("Tell me about a time you scaled a system?");
    let (q1, text) = call.question().await;
    assert_eq!(text, "Tell me about a time you scaled a system?");
    call.server(
        "a fast delta",
        |m| matches!(m, ServerMessage::AnswerDelta(d) if d.question_id == q1 && d.pass == Pass::Fast),
    )
    .await;
    call.answer_done(q1, Pass::Fast).await;
    call.answer_done(q1, Pass::Deep).await;
    call.me_speaks();

    // A typed question: type "other", so the deep pass waits for expand.
    tokio::time::sleep(Duration::from_millis(1_100)).await; // question.force: at most 1/s
    call.handle
        .send(ClientMessage::QuestionForce(QuestionForce {
            text: Some("How would you shard the ledger table?".into()),
        }))
        .await;
    let (q2, text) = call.question().await;
    assert_eq!(text, "How would you shard the ledger table?");
    call.answer_done(q2, Pass::Fast).await;
    call.handle
        .send(ClientMessage::AnswerExpand(copilot_client::protocol::AnswerExpand { question_id: q2 }))
        .await;
    call.answer_done(q2, Pass::Deep).await;
    call.handle
        .send(ClientMessage::AnswerFeedback(copilot_client::protocol::AnswerFeedback {
            question_id: q2,
            rating: copilot_client::protocol::AnswerFeedbackRating::Up,
            reason: None,
        }))
        .await;

    // The hotkey: no text, so the gateway answers what it heard.
    tokio::time::sleep(Duration::from_millis(1_100)).await;
    call.handle.send(ClientMessage::QuestionForce(QuestionForce { text: None })).await;
    let (q3, _) = call.question().await;
    call.answer_done(q3, Pass::Fast).await;

    // The network drops mid-question: the start frame went out, the end frame waits in the
    // ring. After the resume the ring is resent and the gateway drops the duplicate.
    call.say_start("What is a system you designed?");
    tokio::time::sleep(Duration::from_millis(200)).await;
    call.handle.command(Command::DropConnection).await;
    call.say_end();
    let (sid2, resumed) = call.live().await;
    assert_eq!((sid2, resumed), (sid, true));
    let (q4, text) = call.question().await;
    assert_eq!(text, "What is a system you designed?", "words heard once, not twice");
    call.answer_done(q4, Pass::Fast).await;

    call.handle.command(Command::End).await;
    let (reason, _, call) = call.stopped().await;
    assert_eq!(reason, StopReason::Ended);

    // Every server message processed once, in order, with no gap across the resume.
    let seqs: Vec<u64> = call.server_messages().iter().map(|m| m.seq()).collect();
    assert_eq!(seqs, (1..=seqs.len() as u64).collect::<Vec<_>>());
    assert!(
        !call.server_messages().iter().any(|m| matches!(m, ServerMessage::ServerError(_))),
        "{}",
        call.summary()
    );
    let stats = call.handle.stats();
    assert_eq!((stats.connects, stats.resumes, stats.new_sessions), (2, 1, 1));
    assert!(stats.frames_resent >= 2, "{stats:?}");
    assert!(stats.pings_sent >= 3, "{stats:?}");
    assert_eq!(stats.close_codes.last(), Some(&Some(1000)));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_replaced_connection_stops_without_reconnecting() {
    let gw = Gateway::start(0, &[TOKEN], None);
    let mut a = Call::start(&gw.url(), tokens(TOKEN), None);
    let (sid, _) = a.live().await;
    // This client's own newer connection takes the session: the old one gets 4410.
    let code = client::end_session(&gw.url(), &StaticTokens::one(TOKEN), sid).await.unwrap();
    assert_eq!(code, Some(1000));
    let (reason, _, a) = a.stopped().await;
    assert_eq!(reason, StopReason::Replaced);
    let stats = a.handle.stats();
    assert_eq!(stats.close_codes, vec![Some(4410)]);
    assert_eq!(stats.connects, 1, "no reconnect after 4410");
}

#[tokio::test(flavor = "multi_thread")]
async fn a_second_session_gets_4409_until_the_other_is_ended() {
    let gw = Gateway::start(0, &[TOKEN], None);
    let mut a = Call::start(&gw.url(), tokens(TOKEN), None);
    let (sid_a, _) = a.live().await;

    let b = Call::start(&gw.url(), tokens(TOKEN), None);
    let (reason, _, b) = b.stopped().await;
    assert_eq!(reason, StopReason::SessionActive);
    assert_eq!(b.handle.stats().close_codes, vec![Some(4409)]);

    // "End the other session": resume it and send session.end.
    assert_eq!(client::end_session(&gw.url(), &StaticTokens::one(TOKEN), sid_a).await.unwrap(), Some(1000));
    assert_eq!(a.stopped().await.0, StopReason::Replaced);
    let mut c = Call::start(&gw.url(), tokens(TOKEN), None);
    let (sid_c, _) = c.live().await;
    assert_ne!(sid_c, sid_a);
    c.handle.command(Command::End).await;
    assert_eq!(c.stopped().await.0, StopReason::Ended);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_rejected_token_is_refreshed_once() {
    let gw = Gateway::start(0, &[TOKEN], None);
    let good = Token { access_token: TOKEN.into(), expires_at: None };
    let bad = Token { access_token: "stale-token-000000000".into(), expires_at: None };
    let provider = Arc::new(StaticTokens::new(vec![bad.clone(), good]));
    let mut call = Call::start(&gw.url(), provider.clone(), None);
    call.live().await;
    assert_eq!(call.handle.stats().close_codes, vec![Some(4401)]);
    assert_eq!(call.handle.stats().token_refreshes, 1);
    call.handle.command(Command::End).await;
    assert_eq!(call.stopped().await.0, StopReason::Ended);

    // A refresh that yields another bad token stops instead of looping.
    let call = Call::start(&gw.url(), Arc::new(StaticTokens::new(vec![bad])), None);
    let (reason, _, call) = call.stopped().await;
    assert_eq!(reason, StopReason::Unauthorized);
    assert_eq!(call.handle.stats().close_codes, vec![Some(4401), Some(4401)]);
}

#[tokio::test(flavor = "multi_thread")]
async fn auth_expiring_is_answered_with_auth_refresh() {
    // The gateway warns two minutes before expiry; this token expires in 125 s.
    let gw = Gateway::start(0, &["expiring-token-0123456:125", TOKEN], None);
    let provider = Arc::new(StaticTokens::new(vec![
        Token { access_token: "expiring-token-0123456".into(), expires_at: None },
        Token { access_token: TOKEN.into(), expires_at: None },
    ]));
    let mut call = Call::start(&gw.url(), provider, None);
    call.live().await;
    call.server("auth.expiring", |m| matches!(m, ServerMessage::AuthExpiring(_))).await;
    // The gateway accepted the new token: the session goes on, typed questions still work.
    tokio::time::sleep(Duration::from_millis(1_100)).await;
    call.handle
        .send(ClientMessage::QuestionForce(QuestionForce {
            text: Some("What is your biggest strength?".into()),
        }))
        .await;
    let (q, _) = call.question().await;
    call.answer_done(q, Pass::Fast).await;
    assert_eq!(call.handle.stats().token_refreshes, 1);
    call.handle.command(Command::End).await;
    let (reason, _, call) = call.stopped().await;
    assert_eq!(reason, StopReason::Ended);
    assert_eq!(call.handle.stats().close_codes, vec![Some(1000)]);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_gateway_restart_without_session_state_starts_a_new_session() {
    // In-process fake Valkey: the restarted gateway has no record of the session, so the
    // resume closes with 4408 and the client starts over.
    let port = free_port();
    let mut gw = Gateway::start(port, &[TOKEN], None);
    let mut call = Call::start(&gw.url(), tokens(TOKEN), None);
    let (sid, _) = call.live().await;
    gw.kill();
    let _gw = Gateway::start(port, &[TOKEN], None);
    let (sid2, resumed) = call.live().await;
    assert!(!resumed);
    assert_ne!(sid2, sid);
    call.say("Tell me about a time you scaled a system?");
    let (q, _) = call.question().await;
    call.answer_done(q, Pass::Fast).await;
    call.handle.command(Command::End).await;
    let (_, _, call) = call.stopped().await;
    assert!(call.handle.stats().close_codes.contains(&Some(4408)), "{:?}", call.handle.stats());
}

fn valkey_up() -> bool {
    std::net::TcpStream::connect_timeout(&"127.0.0.1:6379".parse().unwrap(), Duration::from_millis(300))
        .is_ok()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_gateway_restart_mid_call_resumes_the_session() {
    if !valkey_up() {
        eprintln!("skipped: no Valkey on localhost:6379 (run `docker compose up -d`)");
        return;
    }
    let valkey = "redis://127.0.0.1:6379/15";
    let port = free_port();
    let mut gw = Gateway::start(port, &[TOKEN], Some(valkey));
    let mut call = Call::start(&gw.url(), tokens(TOKEN), None);
    let (sid, _) = call.live().await;
    call.say("Tell me about a time you scaled a system?");
    let (q1, _) = call.question().await;
    call.answer_done(q1, Pass::Fast).await;

    // The gateway dies; the interviewer asks while it is down. The question waits in the
    // ring, and the client keeps retrying on its backoff.
    gw.kill();
    tokio::time::sleep(Duration::from_millis(300)).await;
    call.say("How would you shard the ledger table?");
    tokio::time::sleep(Duration::from_secs(2)).await;
    let _gw = Gateway::start(port, &[TOKEN], Some(valkey));

    let (sid2, resumed) = call.live().await;
    assert_eq!((sid2, resumed), (sid, true));
    let (q2, text) = call.question().await;
    assert_eq!(text, "How would you shard the ledger table?");
    call.answer_done(q2, Pass::Fast).await;
    call.handle.command(Command::End).await;
    let (reason, _, call) = call.stopped().await;
    assert_eq!(reason, StopReason::Ended);
    let seqs: Vec<u64> = call.server_messages().iter().map(|m| m.seq()).collect();
    assert_eq!(seqs, (1..=seqs.len() as u64).collect::<Vec<_>>(), "no gap or repeat across the restart");
}
