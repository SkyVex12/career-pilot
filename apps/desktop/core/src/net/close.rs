//! What the client does when the socket closes (ARCHITECTURE.md, "Close codes, reconnect
//! policy and limits"). A pure function of the close code and the client's state, so every
//! row of the table is tested here.

use std::time::Duration;

use copilot_protocol::CloseCode;
use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Delay {
    /// 1001: the server is going away; reconnect at once.
    Now,
    /// The reconnect policy's next step.
    Backoff,
    /// 4429: the `retry_after_ms` from the `error` before the close.
    After(Duration),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    /// 1000, or the socket closed after the user ended the session: show the post-call screen.
    Ended,
    /// 4403: show the upgrade prompt; do not retry.
    NoMinutes,
    /// 4409: offer to end the other session.
    SessionActive,
    /// 4410: a newer connection of this client took the session. Nothing to show.
    Replaced,
    /// 4401 again right after a token refresh: the token is no good.
    Unauthorized,
    /// 4426: this app is older than the server supports. Do not reconnect; update.
    Outdated,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseAction {
    Stop(StopReason),
    /// Reconnect; `resume` the current session, or send a new `session.start`.
    Reconnect {
        resume: bool,
        delay: Delay,
        refresh_token: bool,
    },
}

#[derive(Debug, Clone, Copy, Default)]
pub struct CloseContext {
    /// The client has a session id to resume.
    pub has_session: bool,
    /// The user ended the session (session.end was sent).
    pub ending: bool,
    /// `retry_after_ms` of the last `error` message on this connection.
    pub retry_after_ms: Option<u64>,
    /// The previous close was a 4401 and the token was refreshed since.
    pub refreshed_after_401: bool,
}

/// `code` is None when the connection dropped without a close frame (1006).
pub fn on_close(code: Option<u16>, ctx: CloseContext) -> CloseAction {
    use CloseAction::{Reconnect, Stop};
    let code = code.and_then(CloseCode::from_code);
    if ctx.ending && !matches!(code, Some(CloseCode::NoMinutes)) {
        // session.end was sent; whatever the close, the session is over for the user. If the
        // socket dropped first, the gateway ends the session after the resume window.
        return Stop(StopReason::Ended);
    }
    let resume = ctx.has_session;
    match code {
        Some(CloseCode::Normal) => Stop(StopReason::Ended),
        Some(CloseCode::GoingAway) => Reconnect { resume, delay: Delay::Now, refresh_token: false },
        Some(CloseCode::Unauthorized) if ctx.refreshed_after_401 => Stop(StopReason::Unauthorized),
        Some(CloseCode::Unauthorized) => Reconnect { resume, delay: Delay::Now, refresh_token: true },
        Some(CloseCode::NoMinutes) => Stop(StopReason::NoMinutes),
        // No start or resume in time, or the session to resume is gone: start a new one.
        Some(CloseCode::NoStart) => Reconnect { resume: false, delay: Delay::Backoff, refresh_token: false },
        Some(CloseCode::SessionActive) => Stop(StopReason::SessionActive),
        Some(CloseCode::Replaced) => Stop(StopReason::Replaced),
        Some(CloseCode::ClientOutdated) => Stop(StopReason::Outdated),
        Some(CloseCode::RateLimited) => Reconnect {
            resume,
            delay: ctx.retry_after_ms.map_or(Delay::Backoff, |ms| Delay::After(Duration::from_millis(ms))),
            refresh_token: false,
        },
        Some(CloseCode::Internal) => Reconnect { resume, delay: Delay::Backoff, refresh_token: false },
        // 1006 and anything not in the table: the network or the task died.
        None => Reconnect { resume, delay: Delay::Backoff, refresh_token: false },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn live() -> CloseContext {
        CloseContext { has_session: true, ..Default::default() }
    }

    fn reconnect(resume: bool, delay: Delay) -> CloseAction {
        CloseAction::Reconnect { resume, delay, refresh_token: false }
    }

    #[test]
    fn every_code_in_the_table() {
        let cases = [
            (1000, CloseAction::Stop(StopReason::Ended)),
            (1001, reconnect(true, Delay::Now)),
            (4401, CloseAction::Reconnect { resume: true, delay: Delay::Now, refresh_token: true }),
            (4403, CloseAction::Stop(StopReason::NoMinutes)),
            (4408, reconnect(false, Delay::Backoff)),
            (4409, CloseAction::Stop(StopReason::SessionActive)),
            (4410, CloseAction::Stop(StopReason::Replaced)),
            (4426, CloseAction::Stop(StopReason::Outdated)),
            (4429, reconnect(true, Delay::Backoff)),
            (4500, reconnect(true, Delay::Backoff)),
        ];
        for (code, expected) in cases {
            assert_eq!(on_close(Some(code), live()), expected, "{code}");
        }
        // The table covers every close code the schema defines.
        let covered: Vec<u16> = cases.iter().map(|(c, _)| *c).collect();
        assert_eq!(covered, CloseCode::ALL.map(CloseCode::code).to_vec());
    }

    #[test]
    fn a_dropped_connection_resumes_with_backoff() {
        assert_eq!(on_close(None, live()), reconnect(true, Delay::Backoff));
        assert_eq!(on_close(Some(1006), live()), reconnect(true, Delay::Backoff));
        assert_eq!(on_close(Some(1011), live()), reconnect(true, Delay::Backoff));
    }

    #[test]
    fn without_a_session_reconnects_start_over() {
        let ctx = CloseContext::default();
        assert_eq!(on_close(None, ctx), reconnect(false, Delay::Backoff));
        assert_eq!(on_close(Some(1001), ctx), reconnect(false, Delay::Now));
        assert_eq!(on_close(Some(4500), ctx), reconnect(false, Delay::Backoff));
    }

    #[test]
    fn rate_limited_waits_for_retry_after() {
        let ctx = CloseContext { retry_after_ms: Some(1_500), ..live() };
        assert_eq!(on_close(Some(4429), ctx), reconnect(true, Delay::After(Duration::from_millis(1_500))));
    }

    #[test]
    fn a_second_401_after_refreshing_stops() {
        let ctx = CloseContext { refreshed_after_401: true, ..live() };
        assert_eq!(on_close(Some(4401), ctx), CloseAction::Stop(StopReason::Unauthorized));
    }

    #[test]
    fn after_session_end_any_close_ends() {
        let ctx = CloseContext { ending: true, ..live() };
        for code in [None, Some(1000), Some(1001), Some(4500), Some(4410)] {
            assert_eq!(on_close(code, ctx), CloseAction::Stop(StopReason::Ended), "{code:?}");
        }
        assert_eq!(on_close(Some(4403), ctx), CloseAction::Stop(StopReason::NoMinutes));
    }

    #[test]
    fn replaced_never_reconnects() {
        for ctx in [live(), CloseContext::default(), CloseContext { retry_after_ms: Some(10), ..live() }] {
            assert_eq!(on_close(Some(4410), ctx), CloseAction::Stop(StopReason::Replaced));
        }
    }
}
