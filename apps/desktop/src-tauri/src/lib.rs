//! The Tauri shell: windows, commands, the global hotkey, and settings. All audio, network and
//! storage work happens in copilot_client (apps/desktop/core); the web UI renders what it
//! emits and what the API returns.
//!
//! Every UI event goes to all windows as `copilot://event`, and is kept, compacted, for the
//! current session so a window opened or reloaded mid-call (the overlay) rebuilds the whole
//! conversation with `snapshot` (see `history`).

mod history;
mod hotkeys;
mod layout;
mod release;
mod settings;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use copilot_client::api::{ApiClient, ApiError};
use copilot_client::audio::aec::{EchoCanceller, NoEchoCancellation, WebRtcAec};
use copilot_client::audio::capture::{self, DeviceInfo, DeviceSource};
use copilot_client::audio::pipeline::Levels;
use copilot_client::audio::vad::{ChannelVad, Silero, VadParams};
use copilot_client::auth::{DevTokenProvider, OsSecretStore, SecretStore, TokenProvider};
use copilot_client::engine::{AudioParts, Engine, UiEvent};
use copilot_client::net::client::{self, ClientConfig, ClientStats, ConnectionState, StartParams};
use copilot_client::net::close::StopReason;
use copilot_client::oauth::{self, OAuthTokenProvider};
use copilot_client::protocol::Mode;
use history::History;
use hotkeys::Action;
use layout::MonitorLayout;
use release::UpdateState;
use release::journal::Journal;
use release::schedule::{Activity, AfterStart, Gate, Starting};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use settings::{Environment, Settings, insecure_url_warning};
use tauri::ipc::{InvokeBody, Request};
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, State, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use tauri_plugin_opener::OpenerExt;
use uuid::Uuid;

const EVENT: &str = "copilot://event";
/// The settings changed without the UI asking (the sign-in settings fetched at startup).
const SETTINGS_EVENT: &str = "copilot://settings";
/// The overlay's live comfort prefs (opacity, view, click-through), pushed to the overlay
/// window when a hotkey or a move between monitors changes them.
const OVERLAY_EVENT: &str = "copilot://overlay";
const CREDENTIAL_SERVICE: &str = "com.copilot.desktop";
/// The Credential Manager entry older versions kept the recordings' key in.
const OLD_RECORDING_KEY: &str = "me-audio-key";
struct AppState {
    settings: Mutex<Settings>,
    settings_path: PathBuf,
    secrets: Arc<dyn SecretStore>,
    /// The local stack's dev token. Never used in the cloud environment.
    dev_tokens: Arc<DevTokenProvider>,
    /// The deployed stage's browser sign-in (Google or Cognito), made from `settings.oauth`.
    oauth: Mutex<Option<Arc<OAuthTokenProvider>>>,
    engine: Mutex<Option<Arc<Engine>>>,
    history: Arc<Mutex<History>>,
    /// `desktop-<version>`: the gateway's client_version and the API's x-copilot-client.
    client: String,
    /// The app's clock for the update schedule (release::schedule), and what the user did.
    started: Instant,
    activity: Mutex<Activity>,
    /// A session starting, or an update installing: never both (release::schedule::Gate).
    gate: Gate,
    /// The overlay's live state: whether clicks pass through it, the shortcuts registered now
    /// (so the global-shortcut handler can tell which action fired), and a counter that lets a
    /// burst of move or resize events settle before the new frame is saved.
    overlay: OverlayState,
}

#[derive(Default)]
struct OverlayState {
    click_through: AtomicBool,
    geom_gen: AtomicU64,
    shortcuts: Mutex<Vec<(Shortcut, Action)>>,
}

/// The overlay's comfort prefs for the webview: how see-through it is, whether it is compact,
/// and whether clicks are passing through (so it can show the click-through indicator).
#[derive(Clone, Serialize)]
struct OverlayPrefs {
    opacity: f64,
    compact: bool,
    click_through: bool,
    /// The key that turns click-through off, so the indicator can name it (hotkeys.rs).
    click_through_off: String,
}

impl AppState {
    fn clock(&self) -> Duration {
        self.started.elapsed()
    }

    /// What the update schedule needs: whether a session is running or starting, and when
    /// the user last touched the app or ended a session.
    #[cfg_attr(not(feature = "release"), allow(dead_code))]
    fn activity(&self) -> Activity {
        let session = self.engine.lock().unwrap().is_some() || self.gate.starting();
        Activity { session, ..*self.activity.lock().unwrap() }
    }

    /// For the installer: true if no session runs or starts, and none can start from now on.
    #[cfg_attr(not(feature = "release"), allow(dead_code))]
    fn claim_for_install(&self) -> bool {
        let running = self.engine.lock().unwrap().is_some();
        self.gate.claim_install(running)
    }

    /// For start_session: refuse while an update installs, else mark a session as starting
    /// until the returned guard drops.
    fn begin_start(&self) -> Res<Starting<'_>> {
        let running = self.engine.lock().unwrap().is_some();
        self.gate.begin_start(running).map_err(str::to_string)
    }

    fn engine(&self) -> Result<Arc<Engine>, String> {
        self.engine.lock().unwrap().clone().ok_or_else(|| "no session is running".into())
    }

    fn update_settings(&self, f: impl FnOnce(&mut Settings)) {
        let mut s = self.settings.lock().unwrap();
        f(&mut s);
        if let Err(e) = s.save(&self.settings_path) {
            tracing::warn!("could not save settings: {e}");
        }
    }

    /// The sign-in provider for the configured stage, if the app has signed in to one.
    fn oauth(&self) -> Option<Arc<OAuthTokenProvider>> {
        let cfg = self.settings.lock().unwrap().oauth.clone()?;
        let mut slot = self.oauth.lock().unwrap();
        if let Some(p) = slot.as_ref().filter(|p| *p.config() == cfg) {
            return Some(p.clone());
        }
        let p = Arc::new(OAuthTokenProvider::new(cfg, self.secrets.clone()));
        *slot = Some(p.clone());
        Some(p)
    }

    /// The token source for the current environment: the dev token locally, the sign-in in the
    /// cloud.
    fn tokens(&self) -> Res<Arc<dyn TokenProvider>> {
        let env = self.settings.lock().unwrap().environment;
        match env {
            Environment::Local => Ok(self.dev_tokens.clone()),
            Environment::Cloud => match self.oauth() {
                Some(p) => Ok(p),
                None => Err("Sign in under Settings first.".into()),
            },
        }
    }

    fn api(&self) -> Result<ApiClient, String> {
        let (url, profile) = {
            let s = self.settings.lock().unwrap();
            (s.active_urls().1.to_string(), s.profile.clone())
        };
        Ok(ApiClient::new(&url, self.tokens()?)
            .map_err(|e| e.to_string())?
            .with_client(self.client.clone())
            .with_profile(profile))
    }
}

/// An API error for the UI. A 426 (this app is too old for the server) also starts the
/// update at once.
fn api_error(app: &AppHandle, e: ApiError) -> String {
    if let ApiError::Outdated { min_version, .. } = &e {
        outdated(app, &format!("the API needs {min_version} or newer"));
    }
    e.to_string()
}

/// The server refused this version (close 4426 or HTTP 426).
fn outdated(app: &AppHandle, why: &str) {
    #[cfg(feature = "release")]
    release::updater::urgent(app, why);
    #[cfg(not(feature = "release"))]
    {
        let _ = app;
        tracing::warn!("{why}; this build does not update itself");
    }
}

type Res<T> = Result<T, String>;

/// What the settings screen shows of a release build: the stage it is pinned to.
#[derive(Serialize)]
struct ReleaseView {
    stage: String,
    api_url: String,
}

/// The updater, for the settings screen.
#[derive(Serialize)]
struct UpdatesView {
    /// A release build: it updates itself.
    enabled: bool,
    /// The last check, Unix milliseconds.
    checked_at: Option<u64>,
}

#[derive(Serialize)]
struct SettingsView {
    settings: Settings,
    /// The app's version (the tag's, in a release build).
    version: String,
    /// A release build: no local environment or dev token, the stage's URLs fixed.
    release: Option<ReleaseView>,
    dev_token_set: bool,
    /// The cloud environment's signed-in user (their email), if any.
    signed_in_as: Option<String>,
    /// Whether the current environment can make calls: the dev token set, or signed in.
    ready: bool,
    /// Plain HTTP or WS to another machine, for the active environment.
    warnings: Vec<String>,
    updates: UpdatesView,
    /// Hotkeys that do not parse, and sets of hotkeys bound to one key (hotkeys.rs), for the
    /// settings screen to warn about.
    hotkey_review: hotkeys::Review,
}

#[tauri::command]
fn get_settings(app: AppHandle, state: State<'_, AppState>) -> SettingsView {
    let settings = state.settings.lock().unwrap().clone();
    let dev_token_set = state.dev_tokens.is_set();
    let signed_in_as = state.oauth().and_then(|p| p.signed_in_as());
    let ready = match settings.environment {
        Environment::Local => dev_token_set,
        Environment::Cloud => signed_in_as.is_some(),
    };
    let (gateway, api) = settings.active_urls();
    let warnings = [gateway, api].into_iter().filter_map(insecure_url_warning).collect();
    let hotkey_review = hotkeys::review(&settings);
    SettingsView {
        settings,
        version: app.package_info().version.to_string(),
        release: release::config()
            .map(|r| ReleaseView { stage: r.stage.clone(), api_url: r.api_url.clone() }),
        dev_token_set,
        signed_in_as,
        ready,
        warnings,
        updates: UpdatesView { enabled: release::config().is_some(), checked_at: checked_at(&app) },
        hotkey_review,
    }
}

fn checked_at(app: &AppHandle) -> Option<u64> {
    #[cfg(feature = "release")]
    return app.state::<release::updater::Updates>().checked_at();
    #[cfg(not(feature = "release"))]
    {
        let _ = app;
        None
    }
}

fn check_urls(gateway: &str, api: &str) -> Res<()> {
    if !(gateway.starts_with("ws://") || gateway.starts_with("wss://")) {
        return Err("the gateway URL needs ws:// or wss://".into());
    }
    if !(api.starts_with("http://") || api.starts_with("https://")) {
        return Err("the API URL needs http:// or https://".into());
    }
    Ok(())
}

#[tauri::command]
fn save_settings(
    app: AppHandle,
    state: State<'_, AppState>,
    mut settings: Settings,
    dev_token: Option<String>,
) -> Res<()> {
    if let Some(release) = release::config() {
        if dev_token.is_some() {
            return Err("this build signs in with Google only; it has no dev token".into());
        }
        settings.pin_to_release(release);
    }
    let (gateway, api) = settings.active_urls();
    check_urls(gateway, api)?;
    if let Some(token) = dev_token {
        state.dev_tokens.save(&token).map_err(|e| e.to_string())?;
    }
    let kept = state.settings.lock().unwrap().clone();
    // The sign-in settings belong to the stage they came from: a new cloud API URL needs a new
    // sign-in.
    let oauth = if settings.cloud_api_url.trim() == kept.cloud_api_url.trim() { kept.oauth } else { None };
    // What the app keeps for itself, whatever the screen sent (the per-monitor overlay layouts
    // and the monitor it was last on are the app's, not the screen's).
    state.update_settings(|s| {
        *s = Settings {
            last_session_id: kept.last_session_id,
            profile: kept.profile,
            oauth,
            last_version: kept.last_version,
            pending_update: kept.pending_update,
            layouts: kept.layouts,
            overlay_monitor: kept.overlay_monitor,
            ..settings.clone()
        }
    });
    // A waiting update shows the new mode at once.
    #[cfg(feature = "release")]
    release::updater::refresh_ready(&app);
    // The hotkeys may have been rebound: register the new set.
    register_hotkeys(&app);
    Ok(())
}

#[derive(Serialize)]
struct Devices {
    inputs: Vec<DeviceInfo>,
    outputs: Vec<DeviceInfo>,
}

#[tauri::command]
async fn list_devices() -> Devices {
    tauri::async_runtime::spawn_blocking(|| Devices {
        inputs: capture::input_devices(),
        outputs: capture::output_devices(),
    })
    .await
    .unwrap_or(Devices { inputs: vec![], outputs: vec![] })
}

/// One Copilot API call for the UI's screens (documents, jobs, interviews, sessions, usage).
#[tauri::command]
async fn api_call(
    app: AppHandle,
    state: State<'_, AppState>,
    method: String,
    path: String,
    body: Option<Value>,
) -> Res<Value> {
    state.api()?.call(&method, &path, body.as_ref()).await.map_err(|e| api_error(&app, e))
}

/// A resume or job description from the UI: the file's bytes as the raw body, its kind and
/// name in the `x-kind` and `x-file-name` headers (URI-encoded).
#[tauri::command]
async fn upload_document(app: AppHandle, state: State<'_, AppState>, request: Request<'_>) -> Res<Value> {
    let InvokeBody::Raw(bytes) = request.body() else {
        return Err("expected the file's bytes".into());
    };
    let header = |name: &str| -> Res<String> {
        let raw =
            request.headers().get(name).and_then(|v| v.to_str().ok()).ok_or(format!("missing {name}"))?;
        Ok(percent_decode(raw))
    };
    let kind = header("x-kind")?;
    if !matches!(kind.as_str(), "resume" | "job_description") {
        return Err(format!("cannot upload a {kind}"));
    }
    let file_name = header("x-file-name")?;
    // Optional: what the upload is for, as URI-encoded JSON (application_id, resume_version_id,
    // label); the core refuses anything else.
    let target = match request.headers().get("x-target").and_then(|v| v.to_str().ok()) {
        Some(raw) => serde_json::from_str::<serde_json::Map<String, serde_json::Value>>(&percent_decode(raw))
            .map_err(|e| format!("x-target: {e}"))?,
        None => serde_json::Map::new(),
    };
    state
        .api()?
        .upload_document_for(&kind, &file_name, bytes.clone(), &target)
        .await
        .map_err(|e| api_error(&app, e))
}

/// A company document or work log attachment from the UI: the file's bytes as the raw body,
/// the API path to post it to in `x-api-path` and its name in `x-file-name` (URI-encoded).
#[tauri::command]
async fn upload_company_file(app: AppHandle, state: State<'_, AppState>, request: Request<'_>) -> Res<Value> {
    let InvokeBody::Raw(bytes) = request.body() else {
        return Err("expected the file's bytes".into());
    };
    let header = |name: &str| -> Res<String> {
        let raw =
            request.headers().get(name).and_then(|v| v.to_str().ok()).ok_or(format!("missing {name}"))?;
        Ok(percent_decode(raw))
    };
    let path = header("x-api-path")?;
    if !path.starts_with("/employers/") {
        return Err(format!("cannot upload to {path}"));
    }
    let file_name = header("x-file-name")?;
    state.api()?.upload_raw(&path, &file_name, bytes.clone()).await.map_err(|e| api_error(&app, e))
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && let Some(b) = s.get(i + 1..i + 3).and_then(|h| u8::from_str_radix(h, 16).ok())
        {
            out.push(b);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The cloud environment's browser sign-in: the stage's sign-in settings from
/// `GET /v1/auth/config`, then OAuth with PKCE in the default browser. Returns the email.
#[tauri::command]
async fn sign_in(app: AppHandle, state: State<'_, AppState>) -> Res<String> {
    let api_url = state.settings.lock().unwrap().cloud_api_url.trim().to_string();
    if api_url.is_empty() {
        return Err("Set the cloud API URL and save first.".into());
    }
    let cfg = oauth::fetch_auth_config(&api_url).await.and_then(|c| c.oauth()).map_err(|e| e.to_string())?;
    state.update_settings(|s| s.oauth = Some(cfg));
    let provider = state.oauth().ok_or("no sign-in settings")?;
    let opener = app.clone();
    provider
        .sign_in(move |url| {
            opener
                .opener()
                .open_url(url, None::<&str>)
                .map_err(|e| format!("could not open the browser: {e}"))
        })
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn sign_out(state: State<'_, AppState>) -> Res<()> {
    if let Some(p) = state.oauth() {
        p.sign_out().await.map_err(|e| e.to_string())?;
    }
    state.update_settings(|s| s.profile = None);
    Ok(())
}

/// The profile switch (ARCHITECTURE.md, "Profiles"): the profile every API call and the next
/// session act for; None is the account itself. Refused while a session runs or starts, so a
/// session never changes profile halfway.
#[tauri::command]
async fn set_profile(state: State<'_, AppState>, profile: Option<String>) -> Res<()> {
    let _starting = state.begin_start()?;
    if state.engine.lock().unwrap().is_some() {
        return Err("end the session first".into());
    }
    let profile = profile.map(|p| p.trim().to_string()).filter(|p| !p.is_empty());
    state.update_settings(|s| s.profile = profile);
    Ok(())
}

/// `DELETE /me`. In the cloud the sign-in goes too (on AWS the account deletion also removes
/// the Cognito user); the dev token stays (it is the local gateway's).
#[tauri::command]
async fn delete_account(app: AppHandle, state: State<'_, AppState>) -> Res<Value> {
    let result = state.api()?.delete("/me").await.map_err(|e| api_error(&app, e))?;
    state.update_settings(|s| s.last_session_id = None);
    if state.settings.lock().unwrap().environment == Environment::Cloud
        && let Some(p) = state.oauth()
    {
        let _ = p.sign_out().await;
    }
    Ok(result)
}

#[derive(Debug, Deserialize)]
struct StartRequest {
    mode: Mode,
    job_id: Option<Uuid>,
    interview_id: Option<Uuid>,
    meeting_title: Option<String>,
    #[serde(default)]
    employer_id: Option<Uuid>,
    #[serde(default)]
    agenda: Option<String>,
    #[serde(default)]
    material_ids: Vec<Uuid>,
    platform: String,
    store_transcript: bool,
}

/// Events are numbered per session (`n`), so a window that replays the snapshot applies only
/// the live events after it.
fn emit(app: &AppHandle, history: &Mutex<History>, event: &UiEvent) {
    let Ok(event) = serde_json::to_value(event) else { return };
    let n = history.lock().unwrap().push(event.clone());
    let _ = app.emit(EVENT, serde_json::json!({ "n": n, "event": event }));
}

#[tauri::command]
async fn start_session(app: AppHandle, state: State<'_, AppState>, request: StartRequest) -> Res<()> {
    let _starting = state.begin_start()?;
    let tokens = state.tokens()?;
    tokens.current().await.map_err(|e| e.to_string())?;
    let settings = state.settings.lock().unwrap().clone();
    state.history.lock().unwrap().clear();

    let vad = || -> Res<ChannelVad> {
        Ok(ChannelVad::new(Box::new(Silero::new().map_err(|e| e.to_string())?), VadParams::default()))
    };
    let aec: Box<dyn EchoCanceller> =
        if settings.echo_cancellation { Box::new(WebRtcAec::new()) } else { Box::new(NoEchoCancellation) };
    let parts = AudioParts {
        source: Box::new(DeviceSource::new(settings.mic_device.clone(), settings.output_device.clone())),
        aec,
        them_vad: vad()?,
        me_vad: vad()?,
    };
    let config = ClientConfig::new(
        settings.session_url(),
        StartParams {
            mode: request.mode,
            job_id: request.job_id,
            interview_id: request.interview_id,
            meeting_title: request.meeting_title,
            employer_id: request.employer_id,
            agenda: request.agenda,
            material_ids: request.material_ids,
            platform: request.platform,
            client_version: state.client.clone(),
            store_transcript: request.store_transcript,
        },
    );
    let sink = {
        let app = app.clone();
        let history = state.history.clone();
        Arc::new(move |event: UiEvent| {
            if let UiEvent::Connection { state: ConnectionState::Live { session_id, .. } } = &event {
                let sid = *session_id;
                app.state::<AppState>().update_settings(|s| s.last_session_id = Some(sid));
            }
            if let UiEvent::Stopped { reason: StopReason::Ended | StopReason::Replaced } = &event {
                app.state::<AppState>().update_settings(|s| s.last_session_id = None);
            }
            if let UiEvent::Stopped { reason: StopReason::Outdated } = &event {
                outdated(&app, "the gateway closed the session with 4426");
            }
            emit(&app, &history, &event);
        })
    };
    let engine = Engine::start(config, tokens, parts, sink).map_err(|e| e.to_string())?;
    *state.engine.lock().unwrap() = Some(engine.clone());

    // When the client stops (ended, replaced, no minutes, ...), stop the audio and clear up:
    // the socket is closed for good.
    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        engine.finish().await;
        {
            let state = app2.state::<AppState>();
            let mut slot = state.engine.lock().unwrap();
            if slot.as_ref().is_some_and(|e| Arc::ptr_eq(e, &engine)) {
                *slot = None;
            }
            let now = state.clock();
            state.activity.lock().unwrap().last_session_end = Some(now);
        }
    });
    if let Some(overlay) = app.get_webview_window("overlay") {
        let _ = overlay.show();
    }
    Ok(())
}

/// End the live session. `summarize` (default true) writes the post-call outputs; false keeps
/// the transcript only.
#[tauri::command]
async fn end_session(state: State<'_, AppState>, summarize: Option<bool>) -> Res<()> {
    state.engine()?.request_end(summarize.unwrap_or(true)).await;
    Ok(())
}

/// The 4409 offer: end the session this app last had live, so a new one can start.
#[tauri::command]
async fn end_other_session(state: State<'_, AppState>) -> Res<String> {
    let (url, sid) = {
        let s = state.settings.lock().unwrap();
        (s.session_url(), s.last_session_id)
    };
    let Some(sid) = sid else {
        return Err("The other session was not started from this app. Close it there, or wait two \
                    minutes after it lost its connection."
            .into());
    };
    let tokens = state.tokens()?;
    let code = client::end_session(&url, tokens.as_ref(), sid).await?;
    state.update_settings(|s| s.last_session_id = None);
    Ok(format!("ended session {sid} (close {code:?})"))
}

#[tauri::command]
async fn force_question(state: State<'_, AppState>, text: Option<String>) -> Res<()> {
    state.engine()?.force_question(text).await;
    Ok(())
}

#[tauri::command]
async fn respond(state: State<'_, AppState>, text: Option<String>) -> Res<()> {
    state.engine()?.respond(text).await;
    Ok(())
}

#[tauri::command]
async fn expand_answer(state: State<'_, AppState>, question_id: Uuid) -> Res<()> {
    state.engine()?.expand(question_id).await;
    Ok(())
}

#[tauri::command]
async fn send_feedback(
    state: State<'_, AppState>,
    question_id: Uuid,
    up: bool,
    reason: Option<String>,
) -> Res<()> {
    state.engine()?.feedback(question_id, up, reason).await;
    Ok(())
}

#[tauri::command]
async fn set_mode(state: State<'_, AppState>, mode: Mode) -> Res<()> {
    state.engine()?.set_mode(mode).await;
    Ok(())
}

/// The session's events so far (compacted), whether a session is running, and the number of
/// the next event: a window applies these, then live events from that number on.
#[tauri::command]
fn snapshot(state: State<'_, AppState>) -> (Vec<serde_json::Value>, bool, u64) {
    let (events, next) = state.history.lock().unwrap().snapshot();
    (events, state.engine.lock().unwrap().is_some(), next)
}

#[derive(Serialize)]
struct Live {
    running: bool,
    session_id: Option<Uuid>,
    levels: Option<Levels>,
    stats: Option<ClientStats>,
}

#[tauri::command]
fn live_status(state: State<'_, AppState>) -> Live {
    match state.engine.lock().unwrap().as_ref() {
        Some(e) => {
            Live { running: true, session_id: e.session_id(), levels: e.levels(), stats: Some(e.stats()) }
        }
        None => Live { running: false, session_id: None, levels: None, stats: None },
    }
}

/// A click, key or scroll in a window (throttled by the UI): an update waits for quiet.
#[tauri::command]
fn note_activity(app: AppHandle, state: State<'_, AppState>) {
    let now = state.clock();
    state.activity.lock().unwrap().last_input = Some(now);
    // A waiting update's countdown starts again.
    #[cfg(feature = "release")]
    release::updater::refresh_ready(&app);
    #[cfg(not(feature = "release"))]
    let _ = app;
}

/// Look for an update now (the settings' "Check now").
#[tauri::command]
fn check_updates(app: AppHandle) -> Res<()> {
    #[cfg(feature = "release")]
    {
        app.state::<release::updater::Updates>().ask();
        Ok(())
    }
    #[cfg(not(feature = "release"))]
    {
        let _ = app;
        Err("this build does not update itself".into())
    }
}

/// The updater's state (release builds).
#[tauri::command]
fn update_status(app: AppHandle) -> UpdateState {
    #[cfg(feature = "release")]
    return app.state::<release::updater::Updates>().state();
    #[cfg(not(feature = "release"))]
    {
        let _ = app;
        UpdateState::None
    }
}

/// "Install and restart": the downloaded update, now. Never during a session.
#[tauri::command]
fn install_update(app: AppHandle) -> Res<()> {
    #[cfg(feature = "release")]
    return release::updater::install(&app, true);
    #[cfg(not(feature = "release"))]
    {
        let _ = app;
        Err("this build does not update itself".into())
    }
}

#[tauri::command]
fn show_overlay(app: AppHandle, show: bool) -> Res<()> {
    let w = app.get_webview_window("overlay").ok_or("no overlay window")?;
    if show { w.show() } else { w.hide() }.map_err(|e| e.to_string())
}

/// Register every bound hotkey from settings and remember which action each fired key drives,
/// so the handler can dispatch. Invalid or unbound accelerators are skipped (the settings
/// screen flags them); the click-through off switch always keeps a working key (hotkeys.rs).
fn register_hotkeys(app: &AppHandle) {
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    let state = app.state::<AppState>();
    let to_reg = hotkeys::to_register(&state.settings.lock().unwrap());
    let mut registered = Vec::new();
    for (shortcut, action) in to_reg {
        match gs.register(shortcut) {
            Ok(()) => registered.push((shortcut, action)),
            Err(e) => tracing::warn!("hotkey for {action:?} not registered: {e}"),
        }
    }
    *state.overlay.shortcuts.lock().unwrap() = registered;
}

/// Run the action a hotkey is bound to. Keeps the overlay comfort actions off the force-
/// question path, which still needs a running session.
fn dispatch(app: &AppHandle, action: Action) {
    match action {
        Action::ForceQuestion => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                let engine = app.state::<AppState>().engine.lock().unwrap().clone();
                if let Some(engine) = engine {
                    engine.force_question(None).await;
                }
            });
        }
        Action::ToggleOverlay => toggle_overlay(app),
        Action::ClickThrough => {
            let on = !app.state::<AppState>().overlay.click_through.load(Ordering::SeqCst);
            apply_click_through(app, on);
        }
        Action::ClickThroughOff => apply_click_through(app, false),
        Action::OpacityUp => nudge_opacity(app, 1),
        Action::OpacityDown => nudge_opacity(app, -1),
    }
}

fn overlay_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("overlay")
}

/// The overlay's outer frame right now (physical pixels).
fn overlay_frame(w: &WebviewWindow) -> Option<(i32, i32, u32, u32)> {
    let pos = w.outer_position().ok()?;
    let size = w.inner_size().ok()?;
    Some((pos.x, pos.y, size.width, size.height))
}

/// The monitors that exist now, by their work area (so the overlay never hides under a taskbar).
fn monitor_infos(w: &WebviewWindow) -> Vec<layout::MonitorInfo> {
    w.available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|m| {
            let a = m.work_area();
            layout::MonitorInfo {
                key: layout::monitor_key(
                    m.name().map(String::as_str),
                    a.position.x,
                    a.position.y,
                    a.size.width,
                    a.size.height,
                ),
                x: a.position.x,
                y: a.position.y,
                w: a.size.width,
                h: a.size.height,
            }
        })
        .collect()
}

/// The monitor the overlay is on now and the layout remembered for it, deriving a default from
/// the current frame when that monitor has none saved yet.
fn current_layout(app: &AppHandle) -> Option<(String, MonitorLayout)> {
    let w = overlay_window(app)?;
    let (x, y, fw, fh) = overlay_frame(&w)?;
    let monitors = monitor_infos(&w);
    let idx = layout::monitor_for_window(&monitors, x, y, fw, fh)?;
    let key = monitors[idx].key.clone();
    let saved = app.state::<AppState>().settings.lock().unwrap().layouts.get(&key).copied();
    let l =
        saved.unwrap_or(MonitorLayout { x, y, w: fw, h: fh, opacity: layout::MAX_OPACITY, compact: false });
    Some((key, l))
}

fn store_layout(app: &AppHandle, key: &str, l: MonitorLayout) {
    app.state::<AppState>().update_settings(|s| {
        s.layouts.insert(key.to_string(), l);
        s.overlay_monitor = Some(key.to_string());
    });
}

/// Push the overlay's current prefs to the overlay window (opacity and view it renders, and
/// whether to show the click-through indicator).
fn emit_prefs(app: &AppHandle, l: MonitorLayout) {
    let state = app.state::<AppState>();
    let click_through = state.overlay.click_through.load(Ordering::SeqCst);
    let click_through_off = hotkeys::click_through_off(&state.settings.lock().unwrap());
    let _ = app.emit_to(
        "overlay",
        OVERLAY_EVENT,
        OverlayPrefs { opacity: l.opacity, compact: l.compact, click_through, click_through_off },
    );
}

fn toggle_overlay(app: &AppHandle) {
    let Some(w) = overlay_window(app) else { return };
    if matches!(w.is_visible(), Ok(true)) {
        let _ = w.hide();
    } else {
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// Let clicks pass through the overlay to the window underneath, or stop that. Turning it on
/// also shows the overlay, so the indicator and its "off" key are on screen; turning it off is
/// always reachable from the keyboard (hotkeys.rs), the escape hatch from a click-through state.
fn apply_click_through(app: &AppHandle, on: bool) {
    let Some(w) = overlay_window(app) else { return };
    if on {
        let _ = w.show();
    }
    let _ = w.set_ignore_cursor_events(on);
    app.state::<AppState>().overlay.click_through.store(on, Ordering::SeqCst);
    let l = current_layout(app).map(|(_, l)| l).unwrap_or(MonitorLayout {
        x: 0,
        y: 0,
        w: layout::DEFAULT_W,
        h: layout::DEFAULT_H,
        opacity: layout::MAX_OPACITY,
        compact: false,
    });
    emit_prefs(app, l);
}

fn nudge_opacity(app: &AppHandle, steps: i32) {
    let Some((key, mut l)) = current_layout(app) else { return };
    l.opacity = layout::step_opacity(l.opacity, steps);
    store_layout(app, &key, l);
    emit_prefs(app, l);
}

/// Debounced save of the overlay's frame as it is dragged or resized: a burst of events bumps
/// a counter, and only the last one (after a short quiet) writes, so settings are not churned.
fn schedule_remember(app: &AppHandle) {
    let generation = app.state::<AppState>().overlay.geom_gen.fetch_add(1, Ordering::SeqCst) + 1;
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(600)).await;
        if app.state::<AppState>().overlay.geom_gen.load(Ordering::SeqCst) == generation {
            remember_geometry(&app);
        }
    });
}

/// File the overlay's current frame under the monitor it now sits on, keeping that monitor's
/// remembered opacity and view. When it has crossed to a different monitor, that monitor's
/// saved opacity and view are applied so each screen keeps its own feel.
fn remember_geometry(app: &AppHandle) {
    let Some(w) = overlay_window(app) else { return };
    let Some((x, y, fw, fh)) = overlay_frame(&w) else { return };
    let monitors = monitor_infos(&w);
    let Some(idx) = layout::monitor_for_window(&monitors, x, y, fw, fh) else { return };
    let key = monitors[idx].key.clone();
    let (prev_monitor, saved) = {
        let state = app.state::<AppState>();
        let s = state.settings.lock().unwrap();
        (s.overlay_monitor.clone(), s.layouts.get(&key).copied())
    };
    let mut l =
        saved.unwrap_or(MonitorLayout { x, y, w: fw, h: fh, opacity: layout::MAX_OPACITY, compact: false });
    l.x = x;
    l.y = y;
    l.w = fw;
    l.h = fh;
    store_layout(app, &key, l);
    if prev_monitor.as_deref() != Some(key.as_str()) {
        emit_prefs(app, l);
    }
}

/// Place the overlay where it was last left, recovering onto a monitor that still exists when
/// the one it was on is gone (layout.rs). Called once at startup, before the overlay is shown.
fn place_overlay(app: &AppHandle) {
    let Some(w) = overlay_window(app) else { return };
    let monitors = monitor_infos(&w);
    let (layouts, prefer) = {
        let state = app.state::<AppState>();
        let s = state.settings.lock().unwrap();
        (s.layouts.clone(), s.overlay_monitor.clone())
    };
    if let Some(p) = layout::place(&layouts, &monitors, prefer.as_deref()) {
        let _ = w.set_size(PhysicalSize::new(p.layout.w, p.layout.h));
        let _ = w.set_position(PhysicalPosition::new(p.layout.x, p.layout.y));
        store_layout(app, &p.key, p.layout);
    }
}

/// The overlay's starting prefs, which the overlay window asks for as it mounts.
#[tauri::command]
fn overlay_prefs(app: AppHandle, state: State<'_, AppState>) -> OverlayPrefs {
    let click_through = state.overlay.click_through.load(Ordering::SeqCst);
    let click_through_off = hotkeys::click_through_off(&state.settings.lock().unwrap());
    let (opacity, compact) =
        current_layout(&app).map(|(_, l)| (l.opacity, l.compact)).unwrap_or((layout::MAX_OPACITY, false));
    OverlayPrefs { opacity, compact, click_through, click_through_off }
}

/// The opacity slider in settings, and the overlay's opacity hotkeys through `nudge_opacity`.
#[tauri::command]
fn set_overlay_opacity(app: AppHandle, value: f64) {
    let Some((key, mut l)) = current_layout(&app) else { return };
    l.opacity = layout::clamp_opacity(value);
    store_layout(&app, &key, l);
    emit_prefs(&app, l);
}

/// The overlay's compact toggle: remember the view for the monitor it is on.
#[tauri::command]
fn set_overlay_view(app: AppHandle, compact: bool) {
    let Some((key, mut l)) = current_layout(&app) else { return };
    l.compact = compact;
    store_layout(&app, &key, l);
}

/// The overlay's click-through button (turning it on; the off switch is a hotkey).
#[tauri::command]
fn set_click_through(app: AppHandle, on: bool) {
    apply_click_through(&app, on);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    let builder = tauri::Builder::default();
    #[cfg(feature = "release")]
    let builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
    builder
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    // Find which action this key is bound to now, then run it.
                    let action = app
                        .state::<AppState>()
                        .overlay
                        .shortcuts
                        .lock()
                        .unwrap()
                        .iter()
                        .find(|(sc, _)| sc == shortcut)
                        .map(|(_, a)| *a);
                    if let Some(action) = action {
                        dispatch(app, action);
                    }
                })
                .build(),
        )
        .setup(|app| {
            let dir = app.path().app_config_dir()?;
            let settings_path = dir.join("settings.json");
            let mut settings = Settings::load(&settings_path);
            if let Some(release) = release::config() {
                settings.pin_to_release(release);
            }
            let version = app.package_info().version.to_string();
            let journal = Journal::new(app.path().app_log_dir()?.join("updates.log"));
            // After an update's restart: the "Updated to" note, or a log line that it did not take.
            let after = release::schedule::after_start(
                settings.last_version.as_deref(),
                settings.pending_update.as_deref(),
                &version,
            );
            let updated = match &after {
                AfterStart::Updated { from } => {
                    let from = from.as_deref().unwrap_or("an older version");
                    journal.write(&format!("updated to {version} (from {from})"));
                    true
                }
                AfterStart::DidNotTake { version: wanted } => {
                    journal.write(&format!("the update to {wanted} did not complete; still on {version}"));
                    false
                }
                AfterStart::Nothing => false,
            };
            if release::config().is_some() {
                journal.write(&format!("started {version}"));
            }
            settings.last_version = Some(version.clone());
            settings.pending_update = None;
            if let Err(e) = settings.save(&settings_path) {
                tracing::warn!("could not save settings: {e}");
            }
            let needs_auth_config = release::config().is_some() && settings.oauth.is_none();
            let secrets: Arc<dyn SecretStore> = Arc::new(OsSecretStore::new(CREDENTIAL_SERVICE));
            // Versions before live transcription recorded the user's audio for an upload after
            // the call; the gateway no longer takes those, so what is left goes, with its key.
            let leftover = app.path().app_local_data_dir()?.join("me-audio");
            if leftover.exists() {
                let _ = std::fs::remove_dir_all(&leftover);
                let _ = secrets.delete(OLD_RECORDING_KEY);
            }
            app.manage(AppState {
                settings: Mutex::new(settings),
                settings_path,
                dev_tokens: Arc::new(DevTokenProvider::new(secrets.clone())),
                oauth: Mutex::new(None),
                secrets,
                engine: Mutex::new(None),
                history: Arc::new(Mutex::new(History::default())),
                client: format!("desktop-{version}"),
                started: Instant::now(),
                activity: Mutex::new(Activity::default()),
                gate: Gate::default(),
                overlay: OverlayState::default(),
            });
            #[cfg(feature = "release")]
            app.manage(release::updater::Updates::new(journal, updated.then(|| version.clone())));
            #[cfg(not(feature = "release"))]
            let _ = (updated, journal);
            register_hotkeys(app.handle());
            // GET /me first (it woke Aurora when Stage 0 was to scale it to zero; now it checks
            // the sign-in).
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                // A release build learns its stage's sign-in settings at once, so a sign-in kept
                // in the Credential Manager (from an earlier install) works without a new one.
                if needs_auth_config {
                    let state = handle.state::<AppState>();
                    let api_url = state.settings.lock().unwrap().cloud_api_url.clone();
                    match oauth::fetch_auth_config(&api_url).await.and_then(|c| c.oauth()) {
                        Ok(cfg) => {
                            state.update_settings(|s| s.oauth = Some(cfg));
                            // The settings screen may have loaded before this answered.
                            let _ = handle.emit(SETTINGS_EVENT, ());
                        }
                        Err(e) => tracing::info!("sign-in settings not fetched at startup: {e}"),
                    }
                }
                if let Ok(api) = handle.state::<AppState>().api()
                    && let Err(e) = api.get("/me").await
                {
                    tracing::info!("GET /me at startup: {}", api_error(&handle, e));
                }
            });
            // Transparent so the opacity setting can make the overlay see-through to the window
            // underneath (the panel paints its own translucent background in styles.css).
            let overlay =
                WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("index.html#overlay".into()))
                    .title("Copilot overlay")
                    .inner_size(440.0, 620.0)
                    .min_inner_size(320.0, 240.0)
                    .always_on_top(true)
                    .decorations(false)
                    .skip_taskbar(true)
                    .transparent(true)
                    .visible(false)
                    .build()?;
            // Save the overlay's frame per monitor as it is dragged or resized (debounced).
            let handle = app.handle().clone();
            overlay.on_window_event(move |event| {
                if matches!(event, WindowEvent::Moved(_) | WindowEvent::Resized(_)) {
                    schedule_remember(&handle);
                }
            });
            // Reopen where it was last left, recovering onto a monitor that still exists.
            place_overlay(app.handle());
            #[cfg(feature = "release")]
            release::updater::start(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_settings,
            save_settings,
            list_devices,
            api_call,
            upload_document,
            upload_company_file,
            delete_account,
            sign_in,
            sign_out,
            set_profile,
            start_session,
            end_session,
            end_other_session,
            force_question,
            respond,
            expand_answer,
            send_feedback,
            set_mode,
            snapshot,
            live_status,
            show_overlay,
            overlay_prefs,
            set_overlay_opacity,
            set_overlay_view,
            set_click_through,
            update_status,
            install_update,
            check_updates,
            note_activity,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::percent_decode;

    #[test]
    fn file_names_are_uri_decoded() {
        assert_eq!(percent_decode("R%C3%A9sum%C3%A9%20Alex.pdf"), "Résumé Alex.pdf");
        assert_eq!(percent_decode("plain.md"), "plain.md");
        assert_eq!(percent_decode("bad%zz.txt"), "bad%zz.txt");
    }
}
