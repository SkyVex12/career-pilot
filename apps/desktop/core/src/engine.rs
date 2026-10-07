//! One live session: the audio thread feeding the gateway client, and everything the UI shows
//! as a stream of [`UiEvent`]s.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use copilot_protocol::{
    AnswerExpand, AnswerFeedback, AnswerFeedbackRating, Channel, ClientMessage, Mode, ModeSet, QuestionForce,
};
use serde::Serialize;
use tokio::sync::mpsc;
use uuid::Uuid;

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
use crate::recording::MeAudioSink;

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
    pub recorder: Box<dyn MeAudioSink>,
}

pub struct Engine {
    client: ClientHandle,
    audio: Mutex<Option<AudioThread>>,
    latency: Arc<Mutex<LatencyTracker>>,
    levels: Option<Arc<Mutex<Levels>>>,
    client_task: Mutex<Option<tokio::task::JoinHandle<StopReason>>>,
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
        let (events_tx, mut events_rx) = mpsc::unbounded_channel();
        let (client, task) = client::spawn(config, tokens, events_tx);
        let latency = Arc::new(Mutex::new(LatencyTracker::default()));
        let session_id = Arc::new(Mutex::new(None));
        // The recording of the user's audio follows the session ids the client goes live with.
        let bind_recording = audio.recorder.session_binder();

        // Audio thread: frames to the client, speech edges to the UI and the latency tracker.
        let pipeline = Pipeline::new(audio.aec, audio.them_vad, audio.me_vad, audio.recorder);
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
            tokio::spawn(async move {
                while let Some(event) = events_rx.recv().await {
                    let now = Instant::now();
                    match event {
                        ClientEvent::Server { message } => {
                            let record = latency.lock().unwrap().on_message(&message, now);
                            sink(UiEvent::Server { message });
                            if let Some(question) = record {
                                sink(UiEvent::Latency { question });
                            }
                        }
                        ClientEvent::Connection(state) => {
                            match &state {
                                ConnectionState::Live { session_id: sid, .. } => {
                                    *session_id.lock().unwrap() = Some(*sid);
                                    if let Some(bind) = &bind_recording {
                                        bind(*sid);
                                    }
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

        Ok(Arc::new(Self {
            client,
            audio: Mutex::new(Some(audio_thread)),
            latency,
            levels,
            client_task: Mutex::new(Some(task)),
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
        self.latency.lock().unwrap().on_force(Instant::now());
        let text = text.map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
        self.send(ClientMessage::QuestionForce(QuestionForce { text })).await;
    }

    pub async fn expand(&self, question_id: Uuid) {
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

    /// Test hook: drop the socket as a network failure would.
    pub async fn drop_connection(&self) {
        self.client.command(Command::DropConnection).await;
    }

    /// Ask for `session.end` without waiting; whoever awaits [`Engine::finish`] sees the stop.
    pub async fn request_end(&self) {
        self.client.command(Command::End).await;
    }

    /// End the session (`session.end`), stop the audio, and wait for the client to finish.
    pub async fn end(&self) -> Option<StopReason> {
        self.client.command(Command::End).await;
        self.finish().await
    }

    /// Stop without ending the session; it stays resumable on the gateway for 120 s.
    pub async fn shutdown(&self) -> Option<StopReason> {
        self.client.command(Command::Shutdown).await;
        self.finish().await
    }

    /// Wait for the client to stop on its own (1000, 4410, ...), then stop the audio.
    pub async fn finish(&self) -> Option<StopReason> {
        let task = self.client_task.lock().unwrap().take();
        let reason = match task {
            Some(t) => t.await.ok(),
            None => None,
        };
        self.stop_audio();
        reason
    }

    pub fn stop_audio(&self) {
        if let Some(mut a) = self.audio.lock().unwrap().take() {
            a.stop();
        }
    }
}
