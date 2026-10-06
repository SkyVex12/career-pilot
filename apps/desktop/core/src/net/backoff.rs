//! Reconnect backoff (ARCHITECTURE.md, "Close codes, reconnect policy and limits"): 250 ms,
//! 500 ms, 1 s, 2 s, then every 5 s, each with random jitter of up to 20%.

use std::time::Duration;

use rand::Rng;

const STEPS_MS: [u64; 4] = [250, 500, 1_000, 2_000];
const STEADY_MS: u64 = 5_000;
pub const JITTER: f64 = 0.2;

#[derive(Debug, Default, Clone)]
pub struct Backoff {
    attempt: usize,
}

impl Backoff {
    pub fn new() -> Self {
        Self::default()
    }

    /// Attempts since the last reset.
    pub fn attempt(&self) -> usize {
        self.attempt
    }

    /// The base delay for the next attempt, before jitter.
    pub fn base(&self) -> Duration {
        Duration::from_millis(STEPS_MS.get(self.attempt).copied().unwrap_or(STEADY_MS))
    }

    /// The delay before the next attempt, with up to 20% jitter added, and advance.
    pub fn next_delay(&mut self, rng: &mut impl Rng) -> Duration {
        let base = self.base();
        self.attempt += 1;
        base.mul_f64(1.0 + rng.random_range(0.0..=JITTER))
    }

    /// After a connection that worked, start again from 250 ms.
    pub fn reset(&mut self) {
        self.attempt = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rand::SeedableRng;
    use rand::rngs::StdRng;

    #[test]
    fn follows_the_documented_steps() {
        let mut b = Backoff::new();
        let bases: Vec<u64> = (0..7)
            .map(|_| {
                let ms = b.base().as_millis() as u64;
                b.attempt += 1;
                ms
            })
            .collect();
        assert_eq!(bases, vec![250, 500, 1_000, 2_000, 5_000, 5_000, 5_000]);
    }

    #[test]
    fn jitter_adds_at_most_twenty_percent() {
        let mut rng = StdRng::seed_from_u64(1);
        for _ in 0..200 {
            let mut b = Backoff::new();
            for base in [250u64, 500, 1_000, 2_000, 5_000, 5_000] {
                let d = b.next_delay(&mut rng).as_secs_f64() * 1000.0;
                assert!(d >= base as f64 && d <= base as f64 * 1.2 + 1e-6, "{d} for {base}");
            }
        }
    }

    #[test]
    fn jitter_actually_varies() {
        let mut rng = StdRng::seed_from_u64(2);
        let delays: std::collections::BTreeSet<u128> =
            (0..20).map(|_| Backoff::new().next_delay(&mut rng).as_micros()).collect();
        assert!(delays.len() > 10);
    }

    #[test]
    fn reset_starts_over() {
        let mut rng = StdRng::seed_from_u64(3);
        let mut b = Backoff::new();
        for _ in 0..5 {
            b.next_delay(&mut rng);
        }
        assert_eq!(b.base(), Duration::from_secs(5));
        b.reset();
        assert_eq!(b.base(), Duration::from_millis(250));
        assert_eq!(b.attempt(), 0);
    }
}
