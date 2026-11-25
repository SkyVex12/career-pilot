//! Quit removes the app immediately; session cleanup finishes in the background.
use crate::{AppState, Res, settings::Settings};
use copilot_client::{auth::TokenProvider, engine::Engine, net::client};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

#[derive(Default)]
pub(crate) struct QuitState {
    busy: AtomicBool,
    pending: Mutex<Option<PendingQuit>>,
}
#[derive(Clone)]
struct PendingQuit {
    engine: Arc<Engine>,
    settings: Settings,
    tokens: Arc<dyn TokenProvider>,
}

pub(crate) fn pending(app: &AppHandle) -> bool {
    app.try_state::<QuitState>()
        .is_some_and(|s| s.busy.load(Ordering::SeqCst) || s.pending.lock().unwrap().is_some())
}

pub(crate) fn busy(app: &AppHandle) -> bool {
    app.try_state::<QuitState>().is_some_and(|s| s.busy.load(Ordering::SeqCst))
}

pub(crate) async fn quit(app: &AppHandle) -> Res<()> {
    let quit = app.state::<QuitState>();
    if quit.busy.swap(true, Ordering::SeqCst) {
        return Err("The call is already ending. Please wait.".into());
    }
    struct Reset<'a>(&'a AtomicBool);
    impl Drop for Reset<'_> {
        fn drop(&mut self) {
            self.0.store(false, Ordering::SeqCst);
        }
    }
    let _reset = Reset(&quit.busy);
    let state = app.state::<AppState>();
    let _gate = state.gate.begin_quit();
    // Hide before the first await: network latency must never hold the Quit UI open.
    for window in app.webview_windows().values() {
        let _ = window.hide();
    }
    if let Some(tray) = app.tray_by_id("ruby-background") {
        let _ = tray.set_visible(false);
    }
    let result = finish(app, &state, &quit).await;
    drop(_gate);
    drop(_reset);
    if let Err(error) = &result {
        if let Some(tray) = app.tray_by_id("ruby-background") {
            let _ = tray.set_visible(true);
        }
        // A failed end remains retryable instead of silently orphaning a live call.
        let _ = crate::background::open(app);
        if app.get_webview_window("main").is_some_and(|w| w.is_visible().unwrap_or(false)) {
            let _ = app.emit_to("main", crate::OPEN_CALL_EVENT, ());
        }
        let _ = app.emit("copilot://quit-error", error);
    }
    result
}

async fn finish(app: &AppHandle, state: &AppState, quit: &QuitState) -> Res<()> {
    // A call may still be building audio/network resources when Quit is pressed.
    tokio::time::timeout(Duration::from_secs(15), async {
        while state.gate.starting() {
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .map_err(|_| "The call is still starting. Try Exit again in a moment.")?;
    // Retain the original transport/account when a network failure requires a second attempt.
    let previous = quit.pending.lock().unwrap().clone();
    let target = if previous.is_some() {
        previous
    } else {
        let engine = state.engine.lock().unwrap().clone();
        engine
            .map(|engine| -> Res<PendingQuit> {
                let settings = state.settings.lock().unwrap().clone();
                let tokens = state.tokens()?;
                Ok(PendingQuit { engine, settings, tokens })
            })
            .transpose()?
    };
    if let Some(target) = target {
        *quit.pending.lock().unwrap() = Some(target.clone());
        target.engine.stop_audio();
        tokio::time::timeout(Duration::from_secs(15), target.engine.end())
            .await
            .map_err(|_| "The call has not finished ending. Check your connection and try Exit again.")?;
        let stats = target.engine.stats();
        let sid = target.engine.session_id().or_else(|| stats.session_ids.last().copied());
        // A dropped socket or an end timeout does not acknowledge the server-side end.
        // Reconnect only to this exact call; never end an unrelated last-session ID.
        if let Some(sid) = sid {
            if !end_confirmed(stats.close_codes.last().copied().flatten()) {
                let url = state.fill_url(app, &target.settings.session_url(), true).await?;
                let code = tokio::time::timeout(
                    Duration::from_secs(15),
                    client::end_session(&url, target.tokens.as_ref(), sid),
                )
                .await
                .map_err(
                    |_| "Could not confirm that the call ended. Check your connection and try Exit again.",
                )??;
                if !end_confirmed(code) {
                    return Err("The server did not confirm that the call ended. Try Exit again.".into());
                }
            }
            state.update_settings(|s| {
                if s.last_session_id == Some(sid) {
                    s.last_session_id = None;
                }
            });
        }
        *quit.pending.lock().unwrap() = None;
    }
    // Leaving a guest seat must never send session.end for the host.
    if let Some(seat) = state.seat.lock().unwrap().take() {
        seat.leave();
    }
    crate::remote::end();
    crate::background::mark_exiting(app);
    app.exit(0);
    Ok(())
}

fn end_confirmed(code: Option<u16>) -> bool {
    use copilot_client::protocol::CloseCode;
    matches!(
        code.and_then(CloseCode::from_code),
        Some(CloseCode::Normal | CloseCode::NoStart | CloseCode::NoMinutes)
    )
}

/// Native close and native menu callbacks cannot await. Errors remain visible and retryable.
pub(crate) fn request(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        if let Err(error) = quit(&app).await {
            tracing::warn!("Quit: {error}");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quitting_blocks_new_sessions_and_updates_while_an_existing_start_finishes() {
        let gate = crate::release::schedule::Gate::default();
        let starting = gate.begin_start(false).unwrap();
        let quitting = gate.begin_quit();
        assert!(gate.starting());
        drop(starting);
        assert!(!gate.starting());
        assert!(gate.begin_start(false).is_err());
        assert!(!gate.claim_install(false));
        drop(quitting);
        assert!(gate.begin_start(false).is_ok());
    }
    #[test]
    fn only_a_confirmed_end_or_already_gone_call_allows_exit() {
        assert!(end_confirmed(Some(1000)));
        assert!(!end_confirmed(None));
        assert!(!end_confirmed(Some(1006)));
        assert!(!end_confirmed(Some(4401)));
        assert!(!end_confirmed(Some(4410)));
        assert!(!end_confirmed(Some(1011)));
    }
}
