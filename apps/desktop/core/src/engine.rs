//! One live session: the audio thread feeding the gateway client, and everything the UI shows
//! as a stream of [`UiEvent`]s.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use copilot_protocol::{
    AnswerExpand, AnswerFeedback, AnswerFeedbackRating, AnswerMode, AnswerStyle, AnswerStyleSet, Channel,
    ClientMessage, Mode, ModeSet, QuestionForce, QuestionForceKind, ResponseLength, ScreenImage, Screenshot,
    SessionSetLanguage, SharingSet,
};
use serde::Serialize;
use tokio::sync::{mpsc, watch};
use uuid::Uuid;

use crate::audio::FrameDuration;
use crate::audio::aec::EchoCanceller;
use crate::audio::pipeline::{AudioThread, Levels, Pipeline, VadChange};
use crate::audio::source::{AudioSource, CaptureError};
use crate::audio::vad::ChannelVad;
use crate::auth::TokenProvider;
use crate::latency::{LatencyTracker, QuestionLatency};
use crate::net::client::{
    self, ClientConfig, ClientEvent, ClientHandle, ClientStats, Command, ConnectionState,
};
use crate::net::close::StopReason;

// A few events a second go to the UI, so the size of the largest (a server message) does not
// matter; boxing it would only add an allocation per message.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum UiEvent {
    /// Connection state; drives the overlay badge with `status` messages.
    Connection {
        state: ConnectionState,
    },
    /// A server message, once per `seq`.
    Server {
        message: copilot_protocol::ServerMessage,
    },
    /// The local VAD's view of who is speaking.
    Vad {
        channel: Channel,
        speaking: bool,
        edge_ms: u32,
    },
    Latency {
        question: QuestionLatency,
    },
    Closed {
        code: Option<u16>,
        reason: String,
    },
    Warning {
        message: String,
    },
    /// The session is over; `reason` says why (ended, replaced, ...).
    Stopped {
        reason: StopReason,
    },
}

pub type EventSink = Arc<dyn Fn(UiEvent) + Send + Sync>;

pub struct AudioParts {
    pub source: Box<dyn AudioSource>,
    pub aec: Box<dyn EchoCanceller>,
    pub them_vad: ChannelVad,
    pub me_vad: ChannelVad,
}

pub struct Engine {
    client: ClientHandle,
    audio: Mutex<Option<AudioThread>>,
    latency: Arc<Mutex<LatencyTracker>>,
    levels: Option<Arc<Mutex<Levels>>>,
    completion: watch::Receiver<Option<StopReason>>,
    session_id: Arc<Mutex<Option<Uuid>>>,
}

impl Engine {
    /// Start the client and the audio thread. Must be called inside a tokio runtime.
    pub fn start(
        config: ClientConfig,
        tokens: Arc<dyn TokenProvider>,
        audio: AudioParts,
        sink: EventSink,
    ) -> Result<Arc<Self>, CaptureError> {
        Self::start_with_frame_duration(config, tokens, audio, sink, FrameDuration::default())
    }

    /// Start with explicit audio packetization for an experiment or rollback. All other
    /// capture, VAD, transport and UI behavior is the same as `start`.
    pub fn start_with_frame_duration(
        config: ClientConfig,
        tokens: Arc<dyn TokenProvider>,
        audio: AudioParts,
        sink: EventSink,
        duration: FrameDuration,
    ) -> Result<Arc<Self>, CaptureError> {
        let (events_tx, mut events_rx) = mpsc::unbounded_channel();
        let (client, task) = client::spawn(config, tokens, events_tx);
        let latency = Arc::new(Mutex::new(LatencyTracker::default()));
        let session_id = Arc::new(Mutex::new(None));
        // Audio thread: frames to the client, speech edges to the UI and the latency tracker.
        let pipeline = Pipeline::with_frame_duration(audio.aec, audio.them_vad, audio.me_vad, duration);
        let audio_thread = {
            let client = client.clone();
            let latency = latency.clone();
            let sink = sink.clone();
            AudioThread::start(audio.source, pipeline, move |out| {
                for frame in out.frames {
                    client.send_frame(frame);
                }
                for VadChange { channel, speaking, edge_ms, decided_ms } in out.vad {
                    if channel == Channel::Them && !speaking {
                        let hangover = Duration::from_millis(u64::from(decided_ms.saturating_sub(edge_ms)));
                        let at = out.at.unwrap_or_else(Instant::now);
                        latency.lock().unwrap().on_them_speech_end(at.checked_sub(hangover).unwrap_or(at));
                    }
                    sink(UiEvent::Vad { channel, speaking, edge_ms });
                }
            })
        };
        let audio_thread = match audio_thread {
            Ok(a) => a,
            Err(e) => {
                client.try_send(Command::Shutdown);
                return Err(e);
            }
        };
        let levels = Some(audio_thread.levels.clone());

        // Client events to UI events.
        {
            let latency = latency.clone();
            let session_id = session_id.clone();
            let client = client.clone();
            tokio::spawn(async move {
                while let Some(event) = events_rx.recv().await {
                    let now = Instant::now();
                    match event {
                        ClientEvent::Server { message } => {
                            let (record, reports) = {
                                let mut l = latency.lock().unwrap();
                                (l.on_message(&message, now), l.take_reports())
                            };
                            sink(UiEvent::Server { message });
                            if let Some(question) = record {
                                sink(UiEvent::Latency { question });
                            }
                            // The gateway stores the client's first word with the answer.
                            for report in reports {
                                client.send(ClientMessage::LatencyReport(report)).await;
                            }
                        }
                        ClientEvent::Connection(state) => {
                            match &state {
                                ConnectionState::Live { session_id: sid, .. } => {
                                    *session_id.lock().unwrap() = Some(*sid);
                                }
                                ConnectionState::Stopped { reason } => {
                                    let reason = *reason;
                                    sink(UiEvent::Connection { state });
                                    sink(UiEvent::Stopped { reason });
                                    continue;
                                }
                                _ => {}
                            }
                            sink(UiEvent::Connection { state });
                        }
                        ClientEvent::Closed { code, reason } => sink(UiEvent::Closed { code, reason }),
                        ClientEvent::Warning { message } => sink(UiEvent::Warning { message }),
                    }
                }
            });
        }

        let completion = completion_of(task);
        Ok(Arc::new(Self {
            client,
            audio: Mutex::new(Some(audio_thread)),
            latency,
            levels,
            completion,
            session_id,
        }))
    }

    pub fn session_id(&self) -> Option<Uuid> {
        *self.session_id.lock().unwrap()
    }

    pub fn stats(&self) -> ClientStats {
        self.client.stats()
    }

    pub fn levels(&self) -> Option<Levels> {
        self.levels.as_ref().and_then(|l| l.lock().ok().map(|l| *l))
    }

    pub async fn send(&self, message: ClientMessage) {
        self.client.send(message).await;
    }

    /// The hotkey (no text) or a typed question.
    pub async fn force_question(&self, text: Option<String>) {
        self.force(text, None, None).await;
    }

    /// Ask AI, on a guest's note (seat.note): the note's text as a typed question, with its
    /// id for the session log. The host's choice; a guest can never ask on the call.
    pub async fn ask_note(&self, note_id: Uuid, text: String) {
        self.force(Some(text), None, Some(note_id)).await;
    }

    /// Pause (false) or resume what the call's guests see.
    pub async fn set_sharing(&self, on: bool) {
        self.send(ClientMessage::SharingSet(SharingSet { on })).await;
    }

    /// The Respond button: a suggested reply to what was just said, for a remark that needs
    /// one but is not a question (a request, a reminder, "makes sense?").
    pub async fn respond(&self, text: Option<String>) {
        self.force(text, Some(QuestionForceKind::Respond), None).await;
    }

    async fn force(&self, text: Option<String>, kind: Option<QuestionForceKind>, note_id: Option<Uuid>) {
        self.latency.lock().unwrap().on_force(Instant::now());
        let text = text.map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
        self.send(ClientMessage::QuestionForce(QuestionForce { text, kind, note_id })).await;
    }

    /// Coding mode: the shots of one problem, answered as a question (ARCHITECTURE.md,
    /// "Coding mode"). Its first word is timed from now, as a forced question's is.
    pub async fn screenshot(
        &self,
        question_id: Uuid,
        images: Vec<ScreenImage>,
        answer_mode: AnswerMode,
        language: Option<String>,
    ) {
        self.latency.lock().unwrap().on_force(Instant::now());
        let language = language.map(|l| l.trim().to_string()).filter(|l| !l.is_empty());
        self.send(ClientMessage::Screenshot(Screenshot {
            images,
            question_id: Some(question_id),
            answer_mode: Some(answer_mode),
            language,
            default_language: None,
        }))
        .await;
    }

    pub async fn expand(&self, question_id: Uuid) {
        self.latency.lock().unwrap().on_expand(question_id, Instant::now());
        self.send(ClientMessage::AnswerExpand(AnswerExpand { question_id })).await;
    }

    pub async fn feedback(&self, question_id: Uuid, up: bool, reason: Option<String>) {
        let rating = if up { AnswerFeedbackRating::Up } else { AnswerFeedbackRating::Down };
        let reason = reason.filter(|r| !r.trim().is_empty());
        self.send(ClientMessage::AnswerFeedback(AnswerFeedback { question_id, rating, reason })).await;
    }

    pub async fn set_mode(&self, mode: Mode) {
        self.send(ClientMessage::ModeSet(ModeSet { mode })).await;
    }

    /// Request a live language change. The gateway's correlated event is authoritative.
    pub async fn set_spoken_language(&self, spoken_language: String, request_id: Uuid) {
        self.send(ClientMessage::SessionSetLanguage(SessionSetLanguage { spoken_language, request_id }))
            .await;
    }

    /// The Session tab's detailed-answer style, changed mid-call: applies to the next answers.
    pub async fn set_answer_style(&self, answer_style: AnswerStyle, response_length: Option<ResponseLength>) {
        self.send(ClientMessage::AnswerStyleSet(AnswerStyleSet { answer_style, response_length })).await;
    }

    /// Test hook: drop the socket as a network failure would.
    pub async fn drop_connection(&self) {
        self.client.command(Command::DropConnection).await;
    }

    /// Ask for `session.end` without waiting; whoever awaits [`Engine::finish`] sees the stop.
    /// `summarize: false` keeps the transcript only, with no post-call outputs.
    pub async fn request_end(&self, summarize: bool) {
        self.client.command(Command::End { summarize }).await;
    }

    /// End the session (`session.end`), stop the audio, and wait for the client to finish.
    pub async fn end(&self) -> Option<StopReason> {
        self.client.command(Command::End { summarize: true }).await;
        self.finish().await
    }

    /// Stop without ending the session; it stays resumable on the gateway for 120 s.
    pub async fn shutdown(&self) -> Option<StopReason> {
        self.client.command(Command::Shutdown).await;
        self.finish().await
    }

    /// Wait for the client to stop on its own (1000, 4410, ...), then stop the audio.
    pub async fn finish(&self) -> Option<StopReason> {
        let reason = wait_for_completion(self.completion.clone()).await;
        self.stop_audio();
        reason
    }

    pub fn stop_audio(&self) {
        if let Some(mut a) = self.audio.lock().unwrap().take() {
            a.stop();
        }
    }
}

// All callers must await the same client completion. The desktop already has a cleanup
// waiter when Quit asks to end; taking a JoinHandle let the second waiter return early.
fn completion_of(task: tokio::task::JoinHandle<StopReason>) -> watch::Receiver<Option<StopReason>> {
    let (done, result) = watch::channel(None);
    tokio::spawn(async move {
        if let Ok(reason) = task.await {
            let _ = done.send(Some(reason));
        }
    });
    result
}
async fn wait_for_completion(mut result: watch::Receiver<Option<StopReason>>) -> Option<StopReason> {
    loop {
        if let Some(reason) = *result.borrow_and_update() {
            return Some(reason);
        }
        if result.changed().await.is_err() {
            return None;
        }
    }
}

#[cfg(test)]
mod completion_tests {
    use super::*;
    #[tokio::test]
    async fn quit_and_cleanup_both_wait_for_the_gateway_to_finish() {
        let (ack, received) = tokio::sync::oneshot::channel();
        let result = completion_of(tokio::spawn(async {
            received.await.unwrap();
            StopReason::Ended
        }));
        let cleanup = tokio::spawn(wait_for_completion(result.clone()));
        let quit = tokio::spawn(wait_for_completion(result.clone()));
        tokio::task::yield_now().await;
        assert!(!cleanup.is_finished());
        assert!(!quit.is_finished());
        ack.send(()).unwrap();
        assert_eq!(cleanup.await.unwrap(), Some(StopReason::Ended));
        assert_eq!(quit.await.unwrap(), Some(StopReason::Ended));
        assert_eq!(wait_for_completion(result).await, Some(StopReason::Ended));
    }
    #[tokio::test]
    async fn cancelled_waiter_does_not_lose_completion() {
        let (ack, received) = tokio::sync::oneshot::channel();
        let result = completion_of(tokio::spawn(async {
            received.await.unwrap();
            StopReason::Ended
        }));
        let first = tokio::spawn(wait_for_completion(result.clone()));
        first.abort();
        ack.send(()).unwrap();
        assert_eq!(wait_for_completion(result).await, Some(StopReason::Ended));
    }
}
