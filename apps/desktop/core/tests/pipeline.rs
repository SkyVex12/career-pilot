//! Real speech through the audio pipeline: Silero VAD, the framers and WebRTC AEC, run
//! offline on the fixtures (tests/fixtures/README.md). No devices and no gateway.

use copilot_client::audio::aec::{EchoCanceller, NoEchoCancellation, WebRtcAec};
use copilot_client::audio::pipeline::Pipeline;
use copilot_client::audio::vad::{ChannelVad, Silero, VadParams};
use copilot_client::audio::{OutFrame, TICK_SAMPLES};
use copilot_client::protocol::Channel;
use copilot_client::protocol::audio::{CHANNEL_ME, CHANNEL_THEM, FLAG_SPEECH_END, FLAG_SPEECH_START};

fn load(name: &str) -> Vec<f32> {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    let mut reader = hound::WavReader::open(&path).unwrap();
    let spec = reader.spec();
    assert_eq!((spec.sample_rate, spec.channels, spec.bits_per_sample), (16_000, 1, 16), "{name}");
    reader.samples::<i16>().map(|s| f32::from(s.unwrap()) / 32768.0).collect()
}

fn pipeline(aec: Box<dyn EchoCanceller>) -> Pipeline {
    let vad = || ChannelVad::new(Box::new(Silero::new().unwrap()), VadParams::default());
    Pipeline::new(aec, vad(), vad())
}

struct Run {
    frames: Vec<OutFrame>,
    me_speech_ms: u32,
    me_segments: Vec<(u32, u32)>,
}

/// Feed `them` and `me` tick by tick, plus a second of silence to flush the VAD.
fn run(mut p: Pipeline, them: &[f32], me: &[f32]) -> Run {
    let len = them.len().max(me.len()) + 16_000;
    let pad = |x: &[f32], i: usize| -> Vec<f32> {
        (i..i + TICK_SAMPLES).map(|j| x.get(j).copied().unwrap_or(0.0)).collect()
    };
    let mut frames = Vec::new();
    let mut me_segments = Vec::new();
    let mut me_start = None;
    for i in (0..len).step_by(TICK_SAMPLES) {
        let out = p.process_tick(&pad(them, i), &pad(me, i));
        for v in &out.vad {
            if v.channel == Channel::Me {
                if v.speaking {
                    me_start = Some(v.edge_ms);
                } else if let Some(s) = me_start.take() {
                    me_segments.push((s, v.edge_ms));
                }
            }
        }
        frames.extend(out.frames);
    }
    let me_speech_ms = me_segments.iter().map(|(a, b)| b - a).sum();
    Run { frames, me_speech_ms, me_segments }
}

fn echo_of(far: &[f32], delay_ms: usize, gain: f32) -> Vec<f32> {
    let d = delay_ms * 16;
    // A small room: the direct path and two reflections.
    (0..far.len() + d + 400)
        .map(|i| {
            let tap = |k: usize, g: f32| if i >= k && i - k < far.len() { far[i - k] * g } else { 0.0 };
            tap(d, gain) + tap(d + 96, gain * 0.35) + tap(d + 304, gain * 0.15)
        })
        .collect()
}

#[test]
fn them_speech_becomes_two_segments_of_frames() {
    let them = load("them.wav");
    let r = run(pipeline(Box::new(NoEchoCancellation)), &them, &[]);
    let them_frames: Vec<&OutFrame> = r.frames.iter().filter(|f| f.channel == CHANNEL_THEM).collect();
    let starts = them_frames.iter().filter(|f| f.flags & FLAG_SPEECH_START != 0).count();
    let ends: Vec<&&OutFrame> = them_frames.iter().filter(|f| f.flags & FLAG_SPEECH_END != 0).collect();
    // Two questions, each one segment (TTS has short pauses between words, under 200 ms).
    assert_eq!(
        (starts, ends.len()),
        (2, 2),
        "{:?}",
        them_frames.iter().map(|f| (f.flags, f.capture_ms)).collect::<Vec<_>>()
    );
    assert!(ends.iter().all(|f| f.audio.is_empty()));
    // Speech only: the audio sent is the speech plus pre-roll and hangover, far less than the
    // clip with its long pauses.
    let sent_ms = them_frames.iter().map(|f| f.audio.len()).sum::<usize>() / 16;
    let clip_ms = them.len() / 16;
    assert!(sent_ms > 3_000 && sent_ms < clip_ms - 2_000, "sent {sent_ms} ms of a {clip_ms} ms clip");
    // Frames are at most 1 s, and 100 ms once speech is under way.
    assert!(them_frames.iter().all(|f| f.audio.len() <= 16_000));
    // No me frames: the mic was silent.
    assert!(r.frames.iter().all(|f| f.channel != CHANNEL_ME));
}

#[test]
fn without_headphones_aec_keeps_the_far_end_off_the_me_channel() {
    let them = load("them.wav");
    let mic = echo_of(&them, 60, 0.6);

    let without = run(pipeline(Box::new(NoEchoCancellation)), &them, &mic);
    let with = run(pipeline(Box::new(WebRtcAec::new())), &them, &mic);

    // Without cancellation the echo reads as the user talking over the interviewer...
    assert!(without.me_speech_ms > 2_000, "echo heard as me for only {} ms", without.me_speech_ms);
    // ...with it, at most a blip while AEC3 converges at the start of the call.
    assert!(
        with.me_speech_ms * 10 < without.me_speech_ms && with.me_speech_ms < 400,
        "with AEC: me speech {} ms ({:?}); without: {} ms",
        with.me_speech_ms,
        with.me_segments,
        without.me_speech_ms
    );
}

#[test]
fn with_echo_the_users_own_speech_still_counts() {
    // The user answers right after the first question, while the far end is silent but
    // its echo tail and the room are still there.
    let them = load("them.wav");
    let near = load("me.wav");
    let echo = echo_of(&them, 60, 0.6);
    let offset = 16_000 * 4; // 4 s in: between the two questions
    let mut mic = echo.clone();
    mic.resize(mic.len().max(offset + near.len()), 0.0);
    for (i, s) in near.iter().enumerate() {
        mic[offset + i] += s;
    }
    let r = run(pipeline(Box::new(WebRtcAec::new())), &them, &mic);
    let near_from = 4_000;
    let near_to = 4_000 + (near.len() / 16) as u32;
    let overlap: u32 =
        r.me_segments.iter().map(|&(a, b)| b.min(near_to).saturating_sub(a.max(near_from))).sum();
    assert!(overlap > 1_500, "user speech detected for only {overlap} ms: {:?}", r.me_segments);
    // The user's speech goes out as audio for the gateway's live STT, and only speech: each
    // segment plus its pre-roll and the end decision's hangover, never the silence between.
    let me_frames: Vec<&OutFrame> = r.frames.iter().filter(|f| f.channel == CHANNEL_ME).collect();
    let me_audio_ms = (me_frames.iter().map(|f| f.audio.len()).sum::<usize>() / 16) as u32;
    assert!(me_audio_ms >= overlap, "{me_audio_ms} ms of mic audio sent for {overlap} ms of speech");
    let bound = r.me_speech_ms + 600 * r.me_segments.len() as u32;
    assert!(me_audio_ms <= bound, "{me_audio_ms} ms of mic audio sent, over {bound}");
    let starts = me_frames.iter().filter(|f| f.flags & FLAG_SPEECH_START != 0).count();
    let ends = me_frames.iter().filter(|f| f.flags & FLAG_SPEECH_END != 0).count();
    assert_eq!((starts, ends), (r.me_segments.len(), r.me_segments.len()));
}
