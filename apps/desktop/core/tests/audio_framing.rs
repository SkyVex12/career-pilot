//! Offline packetization comparison using the checked-in synthetic speech corpus and
//! sample-clock schedules. No devices, gateway, providers or wall-clock latency claims.

use std::time::Instant;

use copilot_client::audio::aec::NoEchoCancellation;
use copilot_client::audio::framer::SpeechFramer;
use copilot_client::audio::pipeline::{Pipeline, VadChange};
use copilot_client::audio::vad::{ChannelVad, Silero, VadEvent, VadParams};
use copilot_client::audio::{FRAME_SAMPLES, FrameDuration, OutFrame, SAMPLE_RATE, TICK_SAMPLES};
use copilot_client::protocol::Channel;
use copilot_client::protocol::audio::{CHANNEL_ME, CHANNEL_THEM, FLAG_SPEECH_END, FLAG_SPEECH_START};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const DURATIONS: [FrameDuration; 3] = [FrameDuration::Ms20, FrameDuration::Ms40, FrameDuration::Ms100];

#[derive(Debug, PartialEq)]
struct Sent {
    at: u64,
    regular: bool,
    frame: OutFrame,
}

fn sample_positions(sent: &[Sent], channel: u8) -> Vec<(u64, i16)> {
    sent.iter()
        .filter(|s| s.frame.channel == channel)
        .flat_map(|s| {
            let from = u64::from(s.frame.capture_ms) * u64::from(SAMPLE_RATE) / 1000;
            s.frame.audio.iter().enumerate().map(move |(i, &sample)| (from + i as u64, sample))
        })
        .collect()
}

fn flags(sent: &[Sent]) -> Vec<(u8, u8, u32, u64)> {
    sent.iter()
        .filter(|s| s.frame.flags != 0)
        .map(|s| (s.frame.channel, s.frame.flags, s.frame.capture_ms, s.at))
        .collect()
}

fn report(name: &str, duration: FrameDuration, sent: &[Sent], elapsed_us: u128) -> Value {
    let regular: Vec<_> = sent.iter().filter(|s| s.regular && !s.frame.audio.is_empty()).collect();
    let ages = |frames: Vec<&Sent>| {
        let mut values = Vec::new();
        for s in frames {
            let from = u64::from(s.frame.capture_ms) * u64::from(SAMPLE_RATE) / 1000;
            for i in 0..s.frame.audio.len() as u64 {
                values.push((s.at - (from + i)) as f64 * 1000.0 / f64::from(SAMPLE_RATE));
            }
        }
        values.sort_by(f64::total_cmp);
        assert!(!values.is_empty());
        values
    };
    let regular_ages = ages(regular.clone());
    let all_ages = ages(sent.iter().collect());
    assert!(*regular_ages.last().unwrap() <= f64::from(duration.millis()) + 10.0);
    let stats = |values: &[f64]| {
        json!({
            "n": values.len(),
            "mean": values.iter().sum::<f64>() / values.len() as f64,
            "p50_nearest_rank": values[(values.len() - 1) / 2],
            "p95_nearest_rank": values[(values.len() * 95).div_ceil(100) - 1],
            "max": values.last().unwrap(),
        })
    };
    json!({
        "case": name,
        "frame_ms": duration.millis(),
        "frames": sent.len(),
        "regular_frames": regular.len(),
        "pcm_samples": sent.iter().map(|s| s.frame.audio.len()).sum::<usize>(),
        "regular_sample_clock_age_ms": stats(&regular_ages),
        "all_pcm_sample_clock_age_ms": stats(&all_ages),
        "offline_processing_us": elapsed_us,
        "scope": "Sample capture to framer dispatch on a deterministic 10 ms clock; excludes device/FIFO/OS/network/STT/model/UI. All-PCM statistics retain the same exact sample cohort across durations; regular-age statistics exclude startup burst/end flush and have different sample counts. CPU time is contextual, not a speed comparison."
    })
}

fn synthetic(duration: Option<FrameDuration>, channel: u8) -> Vec<Sent> {
    let mut framer = duration
        .map_or_else(|| SpeechFramer::new(channel), |d| SpeechFramer::with_frame_duration(channel, d));
    // Off-tick VAD decisions, two utterances, and overlapping pre-roll exercise clipping,
    // partial tail flushes and identical flags independently of the chosen packet size.
    let events = [
        VadEvent::Start { edge: 1600, decided: 3648 },
        VadEvent::End { edge: 16_000, decided: 19_584 },
        VadEvent::Start { edge: 22_400, decided: 24_448 },
        VadEvent::End { edge: 33_600, decided: 37_184 },
    ];
    let mut sent = Vec::new();
    for from in (0..38_400u64).step_by(TICK_SAMPLES) {
        let at = from + TICK_SAMPLES as u64;
        let due: Vec<_> = events
            .iter()
            .copied()
            .filter(|e| {
                let decided = match e {
                    VadEvent::Start { decided, .. } | VadEvent::End { decided, .. } => decided,
                };
                *decided > from && *decided <= at
            })
            .collect();
        let pcm: Vec<_> = (from..at).map(|x| ((x * 17) % 30_001) as i16).collect();
        sent.extend(framer.push(&pcm, &due).into_iter().map(|frame| Sent {
            at,
            regular: due.is_empty(),
            frame,
        }));
    }
    sent
}

#[test]
fn duration_accepts_only_the_three_reviewed_choices() {
    assert_eq!(FrameDuration::default(), FrameDuration::Ms100);
    assert_eq!(FrameDuration::default().samples(), FRAME_SAMPLES);
    for d in DURATIONS {
        assert_eq!(FrameDuration::try_from(d.millis()), Ok(d));
    }
    for ms in [0, 1, 10, 30, 50, 99, 101, 1000, u32::MAX] {
        assert!(FrameDuration::try_from(ms).is_err(), "{ms}");
    }
}

#[test]
fn all_frame_sizes_preserve_pcm_positions_preroll_and_immediate_end_flags() {
    for channel in [CHANNEL_THEM, CHANNEL_ME] {
        let baseline = synthetic(None, channel);
        assert_eq!(baseline, synthetic(Some(FrameDuration::Ms100), channel));
        let expected_pcm = sample_positions(&baseline, channel);
        let expected_flags = flags(&baseline);
        assert_eq!(expected_flags.len(), 4);
        assert_eq!(
            expected_flags.iter().map(|f| f.1).collect::<Vec<_>>(),
            [FLAG_SPEECH_START, FLAG_SPEECH_END, FLAG_SPEECH_START, FLAG_SPEECH_END]
        );
        assert_eq!(expected_flags[0].2, 0); // Pre-roll before the first question.
        assert_eq!(expected_flags[2].2, 1224); // Never resend the preceding utterance.
        for end in expected_flags.iter().filter(|(_, flag, _, _)| *flag == FLAG_SPEECH_END) {
            assert_eq!(end.3, (u64::from(end.2) * 16).div_ceil(160) * 160);
        }
        for duration in DURATIONS {
            let sent = synthetic(Some(duration), channel);
            assert_eq!(sample_positions(&sent, channel), expected_pcm);
            assert_eq!(flags(&sent), expected_flags);
            assert!(sent.iter().filter(|s| s.regular).all(|s| s.frame.audio.len() == duration.samples()));
            assert!(
                sent.iter().filter(|s| s.frame.flags == FLAG_SPEECH_END).all(|s| s.frame.audio.is_empty())
            );
            println!(
                "FRAMING_BENCH {}",
                report(&format!("synthetic_two_turns_channel_{channel}"), duration, &sent, 0)
            );
        }
    }
}

fn corpus(duration: Option<FrameDuration>, them: &[f32], me: &[f32]) -> (Vec<Sent>, Vec<VadChange>, u128) {
    let vad = || ChannelVad::new(Box::new(Silero::new().unwrap()), VadParams::default());
    let mut pipeline = match duration {
        Some(d) => Pipeline::with_frame_duration(Box::new(NoEchoCancellation), vad(), vad(), d),
        None => Pipeline::new(Box::new(NoEchoCancellation), vad(), vad()),
    };
    let mut sent = Vec::new();
    let mut changes = Vec::new();
    let started = Instant::now();
    // One second of silence closes any final utterance, just as the existing corpus test.
    for from in (0..them.len().max(me.len()) + SAMPLE_RATE as usize).step_by(TICK_SAMPLES) {
        let tick = |pcm: &[f32]| -> Vec<f32> {
            (from..from + TICK_SAMPLES).map(|i| pcm.get(i).copied().unwrap_or(0.0)).collect()
        };
        let out = pipeline.process_tick(&tick(them), &tick(me));
        let at = (from + TICK_SAMPLES) as u64;
        sent.extend(out.frames.into_iter().map(|frame| {
            let channel = if frame.channel == CHANNEL_ME { Channel::Me } else { Channel::Them };
            let regular = !out.vad.iter().any(|v| v.channel == channel);
            Sent { at, regular, frame }
        }));
        changes.extend(out.vad);
    }
    (sent, changes, started.elapsed().as_micros())
}

#[test]
fn checked_in_speech_corpus_preserves_vad_and_pcm_with_measured_packet_ages() {
    let load = |name: &str| {
        let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
        let bytes = std::fs::read(&path).unwrap();
        let mut wav = hound::WavReader::new(std::io::Cursor::new(bytes.as_slice())).unwrap();
        let spec = wav.spec();
        assert_eq!((spec.sample_rate, spec.channels, spec.bits_per_sample), (16_000, 1, 16));
        let samples = wav.samples::<i16>().map(|s| f32::from(s.unwrap()) / 32768.0).collect::<Vec<_>>();
        println!(
            "FRAMING_CORPUS {}",
            json!({"file": name, "sha256": format!("{:x}", Sha256::digest(&bytes)), "samples": samples.len()})
        );
        samples
    };
    let them = load("them.wav");
    let me = load("me.wav");
    let (baseline, boundaries, _) = corpus(None, &them, &me);
    assert!(boundaries.iter().any(|v| v.channel == Channel::Them && v.speaking));
    assert!(boundaries.iter().any(|v| v.channel == Channel::Me && v.speaking));
    for duration in DURATIONS {
        let (sent, vad, elapsed_us) = corpus(Some(duration), &them, &me);
        assert_eq!(vad, boundaries);
        assert_eq!(flags(&sent), flags(&baseline));
        for channel in [CHANNEL_THEM, CHANNEL_ME] {
            assert_eq!(sample_positions(&sent, channel), sample_positions(&baseline, channel));
        }
        assert!(sent.iter().filter(|s| s.regular).all(|s| s.frame.audio.len() == duration.samples()));
        if duration == FrameDuration::Ms100 {
            assert_eq!(sent, baseline);
        }
        println!("FRAMING_BENCH {}", report("checked_in_them_and_me", duration, &sent, elapsed_us));
    }
}
