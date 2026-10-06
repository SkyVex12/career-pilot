//! The whole client, end to end, with a fake audio source instead of devices: real speech
//! played in real time through the tick clock, AEC, Silero VAD, the framers and the client,
//! into the real gateway (offline runner). The offline STT reads audio bytes as text, so real
//! speech gives a garbled transcript; what this checks is that speech, and only speech,
//! reaches the gateway at the right times and the UI sees it.

mod support;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use copilot_client::audio::aec::WebRtcAec;
use copilot_client::audio::source::FakeSource;
use copilot_client::audio::vad::{ChannelVad, Silero, VadParams};
use copilot_client::auth::StaticTokens;
use copilot_client::engine::{AudioParts, Engine, UiEvent};
use copilot_client::net::client::{ClientConfig, ConnectionState, StartParams};
use copilot_client::net::close::StopReason;
use copilot_client::protocol::{Channel, Mode, Pass, ServerMessage};
use copilot_client::recording::NoRecording;
use support::{Gateway, TOKEN};

fn load(name: &str) -> Vec<f32> {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    hound::WavReader::open(path).unwrap().samples::<i16>().map(|s| f32::from(s.unwrap()) / 32768.0).collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn speech_from_the_fake_source_reaches_the_gateway() {
    let gw = Gateway::start(0, &[TOKEN], None);
    let them = load("them.wav");
    // No headphones: the mic hears the far end 60 ms late.
    let source = FakeSource::with_echo(them.clone(), vec![], 60, 0.5);
    let vad = || ChannelVad::new(Box::new(Silero::new().unwrap()), VadParams::default());
    let parts = AudioParts {
        source: Box::new(source),
        aec: Box::new(WebRtcAec::new()),
        them_vad: vad(),
        me_vad: vad(),
        recorder: Box::new(NoRecording),
    };
    let events: Arc<Mutex<Vec<UiEvent>>> = Arc::default();
    let sink = {
        let events = events.clone();
        Arc::new(move |e: UiEvent| events.lock().unwrap().push(e))
    };
    let config = ClientConfig::new(
        gw.url(),
        StartParams {
            mode: Mode::Interview,
            job_id: None,
            meeting_title: None,
            platform: "test".into(),
            client_version: "test".into(),
            store_transcript: false,
        },
    );
    let engine = Engine::start(config, Arc::new(StaticTokens::one(TOKEN)), parts, sink).unwrap();

    // Play the clip (about 9 s), then a typed question to check answers render.
    tokio::time::sleep(Duration::from_millis(them.len() as u64 / 16 + 1_000)).await;
    engine.force_question(Some("How would you shard the ledger table?".into())).await;
    tokio::time::sleep(Duration::from_secs(2)).await;
    let stats = engine.stats();
    assert_eq!(engine.end().await, Some(StopReason::Ended));

    let events = events.lock().unwrap();
    let vad = |ch: Channel, speaking: bool| {
        events.iter().filter(|e| matches!(e, UiEvent::Vad { channel, speaking: s, .. } if *channel == ch && *s == speaking)).count()
    };
    assert_eq!((vad(Channel::Them, true), vad(Channel::Them, false)), (2, 2), "two questions heard");
    assert_eq!(vad(Channel::Me, true), 0, "the echo was not taken for the user");
    let partials = events
        .iter()
        .filter(|e| matches!(e, UiEvent::Server { message: ServerMessage::TranscriptPartial(_) }))
        .count();
    assert!(partials >= 2, "the gateway transcribed the speech frames ({partials} partials)");
    assert!(events.iter().any(|e| matches!(e, UiEvent::Connection { state: ConnectionState::Live { .. } })));
    let latency = events
        .iter()
        .rev()
        .filter_map(|e| match e {
            UiEvent::Latency { question } if question.fast_first_word_ms.is_some() => Some(question.clone()),
            _ => None,
        })
        .next()
        .expect("a latency record for the typed question");
    assert!(latency.fast_first_word_ms.unwrap() < 2_000);
    assert!(events.iter().any(
        |e| matches!(e, UiEvent::Server { message: ServerMessage::AnswerDone(d) } if d.pass == Pass::Fast)
    ));
    // Speech only: about 9 s of clip, a few seconds of it speech, all sent as frames.
    assert!(stats.frames_sent > 20 && stats.frames_dropped == 0, "{stats:?}");
}
