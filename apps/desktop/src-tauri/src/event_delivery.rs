//! Serial delivery without holding a mutex across synchronous native callbacks.
//!
//! Enqueue while holding the history lock that assigned the event number. Only the
//! caller elected by enqueue drains. Reentrant sinks append and return; delivery
//! never runs under the history or queue mutex. The FIFO is deliberately lossless:
//! it adds no new drop/overflow policy to the existing native streaming path.
use std::collections::VecDeque;
use std::sync::Mutex;

pub(crate) struct OrderedDelivery<T> {
    state: Mutex<State<T>>,
}

struct State<T> {
    pending: VecDeque<T>,
    draining: bool,
}

impl<T> Default for OrderedDelivery<T> {
    fn default() -> Self {
        Self { state: Mutex::new(State { pending: VecDeque::new(), draining: false }) }
    }
}

impl<T> OrderedDelivery<T> {
    /// True elects this caller to drain after releasing the history lock.
    pub(crate) fn enqueue(&self, value: T) -> bool {
        let mut state = self.state.lock().unwrap();
        state.pending.push_back(value);
        if state.draining {
            false
        } else {
            state.draining = true;
            true
        }
    }

    pub(crate) fn drain(&self, mut deliver: impl FnMut(T)) {
        let mut owner = DrainOwner { queue: self, armed: true };
        loop {
            let next = {
                let mut state = self.state.lock().unwrap();
                match state.pending.pop_front() {
                    Some(value) => value,
                    None => {
                        state.draining = false;
                        owner.armed = false;
                        return;
                    }
                }
            };
            deliver(next);
        }
    }
}

/// An emitter panic must not leave every subsequent sink convinced someone is
/// draining. Undelivered items remain queued and the next enqueue resumes them;
/// the panicking delivery itself is not retried (it may already have reached JS).
struct DrainOwner<'a, T> {
    queue: &'a OrderedDelivery<T>,
    armed: bool,
}

impl<T> Drop for DrainOwner<'_, T> {
    fn drop(&mut self) {
        if self.armed {
            self.queue.state.lock().unwrap().draining = false;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};
    use std::thread;
    use std::time::Instant;

    #[test]
    fn reentrant_sink_queues_without_deadlock_or_overtaking() {
        let queue = OrderedDelivery::default();
        assert!(queue.enqueue(0));
        let mut seen = Vec::new();
        queue.drain(|n| {
            seen.push(n);
            if n == 0 {
                assert!(!queue.enqueue(1));
                assert!(!queue.enqueue(2));
            }
        });
        assert_eq!(seen, vec![0, 1, 2]);
        assert!(queue.enqueue(3));
        queue.drain(|n| seen.push(n));
        assert_eq!(seen, vec![0, 1, 2, 3]);
    }

    #[test]
    fn held_first_emission_keeps_concurrent_numbered_events_in_order() {
        let history = Arc::new(Mutex::new(Vec::new()));
        let queue = Arc::new(OrderedDelivery::default());
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let output = Arc::new(Mutex::new(Vec::new()));
        let first = {
            let history = history.clone();
            let queue = queue.clone();
            let entered = entered.clone();
            let release = release.clone();
            let output = output.clone();
            thread::spawn(move || {
                let elected = {
                    let mut stored = history.lock().unwrap();
                    let n = stored.len();
                    stored.push(n);
                    queue.enqueue(n)
                };
                assert!(elected);
                queue.drain(|n| {
                    if n == 0 {
                        entered.wait();
                        release.wait();
                    }
                    output.lock().unwrap().push(n);
                });
            })
        };
        entered.wait();
        let mut workers = Vec::new();
        for _ in 0..4 {
            let history = history.clone();
            let queue = queue.clone();
            workers.push(thread::spawn(move || {
                for _ in 0..250 {
                    let mut stored = history.lock().unwrap();
                    let n = stored.len();
                    stored.push(n);
                    assert!(!queue.enqueue(n));
                }
            }));
        }
        for worker in workers {
            worker.join().unwrap();
        }
        // The snapshot includes queued events; duplicate live deliveries are skipped by n.
        assert_eq!(history.lock().unwrap().len(), 1001);
        assert!(output.lock().unwrap().is_empty());
        assert_eq!(queue.state.lock().unwrap().pending.len(), 1000);
        release.wait();
        first.join().unwrap();
        assert_eq!(*output.lock().unwrap(), (0..1001).collect::<Vec<_>>());
    }

    #[test]
    fn panic_releases_ownership_without_retrying_the_attempted_event() {
        let queue = OrderedDelivery::default();
        assert!(queue.enqueue(0));
        assert!(!queue.enqueue(1));
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            queue.drain(|n| {
                assert_eq!(n, 0);
                panic!("authored emitter failure");
            });
        }));
        assert!(result.is_err());
        assert!(queue.enqueue(2));
        let mut seen = Vec::new();
        queue.drain(|n| seen.push(n));
        assert_eq!(seen, vec![1, 2]);
    }

    #[test]
    fn emission_error_does_not_retry_or_block_following_events() {
        let queue = OrderedDelivery::default();
        assert!(queue.enqueue(0));
        assert!(!queue.enqueue(1));
        let mut attempts = Vec::new();
        queue.drain(|n| {
            attempts.push(n);
            let _ignored_like_existing_tauri_emit: Result<(), ()> = if n == 0 { Err(()) } else { Ok(()) };
        });
        assert_eq!(attempts, vec![0, 1]);
        assert!(!queue.state.lock().unwrap().draining);
    }

    #[test]
    fn callbacks_can_read_history_and_enqueue_a_new_epoch() {
        let history = Mutex::new(Vec::new());
        let queue = OrderedDelivery::default();
        {
            let mut h = history.lock().unwrap();
            h.push((1, 0));
            assert!(queue.enqueue((1, 0)));
        }
        let mut seen = Vec::new();
        queue.drain(|event| {
            seen.push(event);
            if event == (1, 0) {
                let mut h = history.lock().unwrap();
                assert_eq!(*h, vec![(1, 0)]);
                h.clear();
                h.push((2, 0));
                assert!(!queue.enqueue((2, 0)));
            }
        });
        assert_eq!(seen, vec![(1, 0), (2, 0)]);
        assert_eq!(*history.lock().unwrap(), vec![(2, 0)]);
    }

    #[test]
    fn finite_uncontended_dispatch_overhead_is_measured() {
        let queue = OrderedDelivery::default();
        let started = Instant::now();
        let mut total = 0_u64;
        for n in 0..100_000_u64 {
            assert!(queue.enqueue(n));
            queue.drain(|item| total += item);
        }
        let elapsed = started.elapsed();
        assert_eq!(total, 4_999_950_000);
        eprintln!(
            "authored_queue_events=100000 elapsed_us={} ns_per_event={:.1}",
            elapsed.as_micros(),
            elapsed.as_nanos() as f64 / 100000.0
        );
    }
}
