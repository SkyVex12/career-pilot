//! The current session's UI events, kept for a window opened or reloaded mid-call (the
//! overlay), which rebuilds the whole conversation from them with `snapshot`.
//!
//! A long call streams tens of thousands of events (an answer arrives a few words per
//! `answer.delta`, a transcript line as many `transcript.partial`s), so the history keeps them
//! compact, in a form the UI's reducer folds to the same view:
//! - a transcript partial or final replaces the earlier text of its segment, in place;
//! - the deltas of one answer pass merge into its first delta, in place;
//! - the newest VAD edge per channel, and the newest latency record per question, replace the
//!   older one.
//!
//! Entries keep their first position, so the order of segments and questions is the order
//! they appeared in. `next` counts every event pushed, compacted or not: a window applies the
//! snapshot, then only live events numbered from `next` on.

use std::collections::HashMap;

use serde_json::Value;

/// A safety cap on stored entries; an hour-long call compacts to a few thousand.
pub const HISTORY_MAX: usize = 50_000;

#[derive(Default)]
pub struct History {
    entries: Vec<Value>,
    next: u64,
    /// Where the entry for a key (a segment, an answer pass, a channel's VAD, ...) is.
    slots: HashMap<String, usize>,
}

fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

/// The slot an event shares with earlier ones, and whether it merges (append) or replaces.
fn slot(event: &Value) -> Option<(String, bool)> {
    match str_of(event, "kind") {
        "vad" => Some((format!("vad:{}", str_of(event, "channel")), false)),
        "latency" => {
            let q = event.get("question").map(|q| str_of(q, "question_id")).unwrap_or("");
            Some((format!("latency:{q}"), false))
        }
        "server" => {
            let m = event.get("message")?;
            match str_of(m, "type") {
                "transcript.partial" | "transcript.final" => {
                    let seg = m.get("seg_id").and_then(Value::as_u64).unwrap_or(0);
                    Some((format!("seg:{}:{seg}", str_of(m, "channel")), false))
                }
                "answer.delta" => {
                    Some((format!("delta:{}:{}", str_of(m, "question_id"), str_of(m, "pass")), true))
                }
                _ => None,
            }
        }
        _ => None,
    }
}

impl History {
    /// Store an event; returns its number `n`.
    pub fn push(&mut self, event: Value) -> u64 {
        let n = self.next;
        self.next += 1;
        if let Some((key, merge)) = slot(&event)
            && let Some(&i) = self.slots.get(&key)
        {
            let entry = &mut self.entries[i];
            if merge {
                let more = event["message"]["text"].as_str().unwrap_or("").to_string();
                if let Some(Value::String(text)) = entry.pointer_mut("/event/message/text") {
                    text.push_str(&more);
                }
            } else {
                entry["event"] = event;
            }
            entry["n"] = n.into();
            return n;
        }
        if self.entries.len() >= HISTORY_MAX {
            return n;
        }
        if let Some((key, _)) = slot(&event) {
            self.slots.insert(key, self.entries.len());
        }
        self.entries.push(serde_json::json!({ "n": n, "event": event }));
        n
    }

    /// The compacted events, oldest first, and the number of the next event.
    pub fn snapshot(&self) -> (Vec<Value>, u64) {
        (self.entries.clone(), self.next)
    }

    pub fn clear(&mut self) {
        *self = Self::default();
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn server(message: Value) -> Value {
        json!({ "kind": "server", "message": message })
    }

    fn partial(channel: &str, seg: u64, text: &str) -> Value {
        server(
            json!({ "type": "transcript.partial", "seq": 1, "channel": channel, "seg_id": seg, "text": text }),
        )
    }

    fn delta(q: &str, pass: &str, text: &str) -> Value {
        server(json!({ "type": "answer.delta", "seq": 1, "question_id": q, "pass": pass, "text": text }))
    }

    fn texts(h: &History) -> Vec<String> {
        h.snapshot()
            .0
            .iter()
            .map(|e| {
                let m = &e["event"]["message"];
                format!("{}:{}", m["type"].as_str().unwrap_or("?"), m["text"].as_str().unwrap_or(""))
            })
            .collect()
    }

    #[test]
    fn a_long_call_compacts_to_its_lines_and_answers() {
        let mut h = History::default();
        h.push(partial("them", 0, "Tell"));
        h.push(partial("them", 0, "Tell me about"));
        h.push(server(json!({ "type": "question.detected", "seq": 2, "question_id": "q1", "text": "x" })));
        for word in ["- Led ", "the ", "migration\n"] {
            h.push(delta("q1", "fast", word));
            h.push(partial("me", 0, "So"));
        }
        h.push(server(json!({ "type": "transcript.final", "seq": 3, "channel": "them", "seg_id": 0, "text": "Tell me about it.", "start_ms": 0, "end_ms": 9 })));
        h.push(delta("q1", "deep", "Situation"));
        let (entries, next) = h.snapshot();
        assert_eq!(next, 11, "every event is numbered");
        assert_eq!(entries.len(), 5);
        assert_eq!(
            texts(&h),
            vec![
                "transcript.final:Tell me about it.", // in the partial's place
                "question.detected:x",
                "answer.delta:- Led the migration\n", // the fast deltas, merged
                "transcript.partial:So",
                "answer.delta:Situation",
            ]
        );
    }

    #[test]
    fn the_newest_vad_edge_per_channel_is_kept() {
        let mut h = History::default();
        for i in 0..100 {
            h.push(json!({ "kind": "vad", "channel": "them", "speaking": i % 2 == 0, "edge_ms": i }));
        }
        h.push(json!({ "kind": "vad", "channel": "me", "speaking": true, "edge_ms": 5 }));
        let (entries, next) = h.snapshot();
        assert_eq!((entries.len(), next), (2, 101));
        assert_eq!(entries[0]["event"]["edge_ms"], 99);
    }

    #[test]
    fn other_events_are_kept_in_order_and_clear_starts_over() {
        let mut h = History::default();
        h.push(json!({ "kind": "warning", "message": "a" }));
        h.push(json!({ "kind": "warning", "message": "a" }));
        assert_eq!(h.snapshot().0.len(), 2);
        h.clear();
        assert_eq!(h.snapshot(), (vec![], 0));
        assert_eq!(h.push(json!({ "kind": "warning", "message": "b" })), 0);
    }
}
