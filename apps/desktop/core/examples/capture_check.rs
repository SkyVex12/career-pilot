//! Check the capture devices without the app: lists them, captures for a few seconds, and
//! prints how much audio each stream delivered and its level.
//!
//!     cargo run -p copilot-client --example capture_check -- [seconds]
//!
//! Play something (a video, the call) while it runs: "them" only delivers audio while the
//! system output plays something.

use std::sync::mpsc::sync_channel;
use std::time::{Duration, Instant};

use copilot_client::audio::capture::{DeviceSource, input_devices, output_devices};
use copilot_client::audio::source::{AudioSource, Packet};

fn main() {
    let secs: u64 = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(5);
    for (kind, list) in [("microphones", input_devices()), ("outputs (loopback)", output_devices())] {
        println!("{kind}:");
        for d in list {
            println!("  {}{}  [{}]", d.name, if d.is_default { " (default)" } else { "" }, d.id);
        }
    }
    let (tx, rx) = sync_channel(1024);
    let mut source = DeviceSource::new(None, None);
    if let Err(e) = source.start(tx) {
        eprintln!("capture failed: {e}");
        std::process::exit(1);
    }
    let start = Instant::now();
    let (mut them, mut me) = ((0usize, 0f32), (0usize, 0f32));
    while start.elapsed() < Duration::from_secs(secs) {
        if let Ok(p) = rx.recv_timeout(Duration::from_millis(100)) {
            let (slot, s) = match &p {
                Packet::Them(s) => (&mut them, s),
                Packet::Me(s) => (&mut me, s),
            };
            slot.0 += s.len();
            slot.1 = slot.1.max(s.iter().fold(0f32, |m, v| m.max(v.abs())));
        }
    }
    source.stop();
    for (name, (n, peak)) in [("them (system output)", them), ("me (microphone)", me)] {
        println!("{name}: {:.2} s of 16 kHz audio in {secs} s, peak {:.3}", n as f64 / 16_000.0, peak);
    }
}
