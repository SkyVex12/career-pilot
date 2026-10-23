//! The Tauri shell: windows, commands, the global hotkey, and settings. All audio, network and
//! storage work happens in copilot_client (apps/desktop/core); the web UI renders what it
//! emits and what the API returns.
//!
//! Every UI event goes to all windows as `copilot://event`, and is kept, compacted, for the
//! current session so a window opened or reloaded mid-call (the overlay) rebuilds the whole
//! conversation with `snapshot` (see `history`).

mod history;
mod release;
mod settings;

use std::path::PathBuf;
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
use release::UpdateState;
use release::journal::{Entry, Journal};
use release::schedule::{Activity, AfterStart, Gate, Starting};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use settings::{Environment, Settings, insecure_url_warning};
use tauri::ipc::{InvokeBody, Request};
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use tauri_plugin_opener::OpenerExt;
use uuid::Uuid;

const EVENT: &str = "copilot://event";
/// The settings changed without the UI asking (the sign-in settings fetched at startup).
const SETTINGS_EVENT: &str = "copilot://settings";
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
    /// updates.log (release::journal).
    journal: Journal,
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
        let url = self.settings.lock().unwrap().active_urls().1.to_string();
        Ok(ApiClient::new(&url, self.tokens()?).map_err(|e| e.to_string())?.with_client(self.client.clone()))
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
    /// The newest lines of updates.log, oldest first.
    log: Vec<Entry>,
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
    SettingsView {
        settings,
        version: app.package_info().version.to_string(),
        release: release::config()
            .map(|r| ReleaseView { stage: r.stage.clone(), api_url: r.api_url.clone() }),
        dev_token_set,
        signed_in_as,
        ready,
        warnings,
        updates: UpdatesView {
            enabled: release::config().is_some(),
            checked_at: checked_at(&app),
            log: state.journal.lines(20),
        },
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
    let hotkey_changed = state.settings.lock().unwrap().hotkey != settings.hotkey;
    let kept = state.settings.lock().unwrap().clone();
    // The sign-in settings belong to the stage they came from: a new cloud API URL needs a new
    // sign-in.
    let oauth = if settings.cloud_api_url.trim() == kept.cloud_api_url.trim() { kept.oauth } else { None };
    // What the app keeps for itself, whatever the screen sent.
    state.update_settings(|s| {
        *s = Settings {
            last_session_id: kept.last_session_id,
            oauth,
            last_version: kept.last_version,
            pending_update: kept.pending_update,
            ..settings.clone()
        }
    });
    if hotkey_changed {
        register_hotkey(&app, &settings.hotkey)?;
    }
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
    state.api()?.upload_document(&kind, &file_name, bytes.clone()).await.map_err(|e| api_error(&app, e))
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
        settings.active_urls().0.to_string(),
        StartParams {
            mode: request.mode,
            job_id: request.job_id,
            interview_id: request.interview_id,
            meeting_title: request.meeting_title,
            employer_id: request.employer_id,
            agenda: request.agenda,
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

#[tauri::command]
async fn end_session(state: State<'_, AppState>) -> Res<()> {
    state.engine()?.request_end().await;
    Ok(())
}

/// The 4409 offer: end the session this app last had live, so a new one can start.
#[tauri::command]
async fn end_other_session(state: State<'_, AppState>) -> Res<String> {
    let (url, sid) = {
        let s = state.settings.lock().unwrap();
        (s.active_urls().0.to_string(), s.last_session_id)
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
fn note_activity(state: State<'_, AppState>) {
    let now = state.clock();
    state.activity.lock().unwrap().last_input = Some(now);
}

/// Look for an update now (the settings' "Check now").
#[tauri::command]
fn check_updates(app: AppHandle) -> Res<()> {
    #[cfg(feature = "release")]
    {
        app.state::<release::updater::Updates>().check_now();
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

fn register_hotkey(app: &AppHandle, accelerator: &str) -> Res<()> {
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    if accelerator.trim().is_empty() {
        return Ok(());
    }
    let shortcut: Shortcut = accelerator.parse().map_err(|e| format!("hotkey {accelerator:?}: {e}"))?;
    gs.register(shortcut).map_err(|e| format!("hotkey {accelerator:?}: {e}"))
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
                .with_handler(|app, _shortcut, event| {
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    // The hotkey forces a question when detection missed one.
                    let app = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let engine = app.state::<AppState>().engine.lock().unwrap().clone();
                        if let Some(engine) = engine {
                            engine.force_question(None).await;
                        }
                    });
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
            let hotkey = settings.hotkey.clone();
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
                journal: journal.clone(),
            });
            #[cfg(feature = "release")]
            {
                let updates = release::updater::Updates::new(journal);
                if updated {
                    updates.set(app.handle(), UpdateState::Updated { version: version.clone() });
                }
                app.manage(updates);
            }
            #[cfg(not(feature = "release"))]
            let _ = (updated, journal);
            if let Err(e) = register_hotkey(app.handle(), &hotkey) {
                tracing::warn!("{e}");
            }
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
            WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("index.html#overlay".into()))
                .title("Copilot overlay")
                .inner_size(440.0, 620.0)
                .min_inner_size(320.0, 240.0)
                .always_on_top(true)
                .decorations(false)
                .skip_taskbar(true)
                .visible(false)
                .build()?;
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
            start_session,
            end_session,
            end_other_session,
            force_question,
            expand_answer,
            send_feedback,
            set_mode,
            snapshot,
            live_status,
            show_overlay,
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
