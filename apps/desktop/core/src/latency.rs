//! Per-question latency for the debug panel: from the end of the other side's speech to the
//! first answer word on screen, measured on the client, next to the gateway's own
//! `latency_ms` from `answer.done`.
//!
//! The anchor is the true end of speech (the VAD's edge, so its hangover counts, as in the
//! replay tool), or the moment a question was forced with the hotkey or typed. Capture
//! buffering before the VAD (about 40 ms) is not counted.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use copilot_protocol::{LatencyMs, Pass, QuestionType, ServerMessage};
use serde::Serialize;
use uuid::Uuid;

/// A forced question's detection arrives within this of the request.
const FORCE_WINDOW: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Anchor {
    SpeechEnd,
    Forced,
    Unknown,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct QuestionLatency {
    pub question_id: Uuid,
    pub text: String,
    pub question_type: QuestionType,
    pub anchor: Anchor,
    /// Anchor to `question.detected` (the first, speculative or not).
    pub detected_ms: Option<u64>,
    /// Anchor to the first non-empty `answer.delta` of each pass.
    pub fast_first_word_ms: Option<u64>,
    pub deep_first_word_ms: Option<u64>,
    pub fast_gateway: Option<LatencyMs>,
    pub deep_gateway: Option<LatencyMs>,
}

struct Entry {
    anchor_at: Option<Instant>,
    record: QuestionLatency,
}

#[derive(Default)]
pub struct LatencyTracker {
    last_speech_end: Option<Instant>,
    pending_force: Option<Instant>,
    questions: HashMap<Uuid, Entry>,
}

fn ms(from: Option<Instant>, to: Instant) -> Option<u64> {
    from.map(|f| to.saturating_duration_since(f).as_millis() as u64)
}

impl LatencyTracker {
    /// The other side stopped speaking at `edge` (wall time of the VAD edge).
    pub fn on_them_speech_end(&mut self, edge: Instant) {
        self.last_speech_end = Some(edge);
    }

    pub fn on_force(&mut self, now: Instant) {
        self.pending_force = Some(now);
    }

    /// Returns the question's updated record when this message changed it.
    pub fn on_message(&mut self, message: &ServerMessage, now: Instant) -> Option<QuestionLatency> {
        match message {
            ServerMessage::QuestionDetected(q) => {
                if let Some(e) = self.questions.get_mut(&q.question_id) {
                    e.record.text.clone_from(&q.text);
                    e.record.question_type = q.question_type;
                    return Some(e.record.clone());
                }
                let forced = self.pending_force.filter(|t| {
                    now.saturating_duration_since(*t) < FORCE_WINDOW && !q.speculative && q.confidence >= 1.0
                });
                let (anchor, anchor_at) = match (forced, self.last_speech_end) {
                    (Some(t), _) => {
                        self.pending_force = None;
                        (Anchor::Forced, Some(t))
                    }
                    (None, Some(t)) => (Anchor::SpeechEnd, Some(t)),
                    (None, None) => (Anchor::Unknown, None),
                };
                let record = QuestionLatency {
                    question_id: q.question_id,
                    text: q.text.clone(),
                    question_type: q.question_type,
                    anchor,
                    detected_ms: ms(anchor_at, now),
                    fast_first_word_ms: None,
                    deep_first_word_ms: None,
                    fast_gateway: None,
                    deep_gateway: None,
                };
                self.questions.insert(q.question_id, Entry { anchor_at, record: record.clone() });
                Some(record)
            }
            ServerMessage::AnswerDelta(d) if !d.text.trim().is_empty() => {
                let e = self.questions.get_mut(&d.question_id)?;
                let slot = match d.pass {
                    Pass::Fast => &mut e.record.fast_first_word_ms,
                    Pass::Deep => &mut e.record.deep_first_word_ms,
                };
                if slot.is_some() {
                    return None;
                }
                *slot = ms(e.anchor_at, now);
                Some(e.record.clone())
            }
            ServerMessage::AnswerDone(done) => {
                let e = self.questions.get_mut(&done.question_id)?;
                let slot = match done.pass {
                    Pass::Fast => &mut e.record.fast_gateway,
                    Pass::Deep => &mut e.record.deep_gateway,
                };
                *slot = done.latency_ms.clone();
                Some(e.record.clone())
            }
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use copilot_protocol::{AnswerDelta, AnswerDone, QuestionDetected, Usage};

    fn qid() -> Uuid {
        Uuid::parse_str("0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210").unwrap()
    }

    fn detected(speculative: bool, confidence: f64) -> ServerMessage {
        ServerMessage::QuestionDetected(QuestionDetected {
            seq: 1,
            question_id: qid(),
            text: "Tell me about a time?".into(),
            question_type: QuestionType::Behavioral,
            confidence,
            speculative,
        })
    }

    fn delta(pass: Pass, text: &str) -> ServerMessage {
        ServerMessage::AnswerDelta(AnswerDelta { seq: 2, question_id: qid(), pass, text: text.into() })
    }

    #[test]
    fn measures_from_the_end_of_speech_to_the_first_word() {
        let t0 = Instant::now();
        let mut l = LatencyTracker::default();
        l.on_them_speech_end(t0);
        let r = l.on_message(&detected(true, 0.9), t0 + Duration::from_millis(400)).unwrap();
        assert_eq!((r.anchor, r.detected_ms), (Anchor::SpeechEnd, Some(400)));
        assert!(l.on_message(&delta(Pass::Fast, "  "), t0 + Duration::from_millis(500)).is_none());
        let r = l.on_message(&delta(Pass::Fast, "- Led"), t0 + Duration::from_millis(900)).unwrap();
        assert_eq!(r.fast_first_word_ms, Some(900));
        // Later deltas do not move it; the confirmation keeps the record.
        assert!(l.on_message(&delta(Pass::Fast, " more"), t0 + Duration::from_millis(950)).is_none());
        let r = l.on_message(&detected(false, 0.9), t0 + Duration::from_millis(960)).unwrap();
        assert_eq!(r.fast_first_word_ms, Some(900));
        let r = l.on_message(&delta(Pass::Deep, "Situation"), t0 + Duration::from_millis(2_100)).unwrap();
        assert_eq!(r.deep_first_word_ms, Some(2_100));
    }

    #[test]
    fn a_forced_question_measures_from_the_hotkey() {
        let t0 = Instant::now();
        let mut l = LatencyTracker::default();
        l.on_them_speech_end(t0);
        l.on_force(t0 + Duration::from_secs(3));
        let r = l.on_message(&detected(false, 1.0), t0 + Duration::from_millis(3_050)).unwrap();
        assert_eq!((r.anchor, r.detected_ms), (Anchor::Forced, Some(50)));
    }

    #[test]
    fn keeps_the_gateway_latency() {
        let t0 = Instant::now();
        let mut l = LatencyTracker::default();
        l.on_message(&detected(false, 0.9), t0);
        let done = ServerMessage::AnswerDone(AnswerDone {
            seq: 3,
            question_id: qid(),
            pass: Pass::Fast,
            memory_refs: vec![],
            usage: Usage { input_tokens: 1, cached_tokens: 0, output_tokens: 1 },
            source: None,
            latency_ms: Some(LatencyMs {
                turn_end_ms: Some(410),
                detector_ms: None,
                first_token_ms: Some(300),
                first_delta_ms: Some(700),
                total_ms: Some(1_400),
            }),
        });
        let r = l.on_message(&done, t0).unwrap();
        assert_eq!(r.anchor, Anchor::Unknown);
        assert_eq!(r.fast_gateway.unwrap().first_delta_ms, Some(700));
    }
}
