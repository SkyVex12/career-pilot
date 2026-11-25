//! The Tauri shell: windows, commands, the global hotkey, and settings. All audio, network and
//! storage work happens in copilot_client (apps/desktop/core); the web UI renders what it
//! emits and what the API returns.
//!
//! Every UI event goes to all windows as `copilot://event`, and is kept, compacted, for the
//! current session so a window opened or reloaded mid-call (the overlay) rebuilds the whole
//! conversation with `snapshot` (see `history`).

mod background;
mod call_window;
mod compact_window;
mod history;
mod hotkeys;
mod layout;
mod native_screen;
mod program_name;
mod release;
mod remote;
#[cfg(any(target_os = "macos", test))]
mod remote_macos;
mod settings;
mod shots;
mod shutdown;
mod stealth;
mod tray_menu;
mod voice;
mod window_drag;
mod window_visibility;
mod workspace_window;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use copilot_client::api::{ApiClient, ApiError};
use copilot_client::audio::aec::{EchoCanceller, NoEchoCancellation, WebRtcAec};
use copilot_client::audio::capture::{self, DeviceInfo, DeviceSource};
use copilot_client::audio::check;
use copilot_client::audio::pipeline::Levels;
use copilot_client::audio::source::{AudioSource, Packet, SilentSource};
use copilot_client::audio::vad::{ChannelVad, Silero, VadParams};
use copilot_client::auth::BoxFuture;
use copilot_client::auth::{DevTokenProvider, OsSecretStore, SecretStore, TokenProvider};
use copilot_client::careerossignin::{CareerOsFailure, CareerOsOutcome, CareerOsTokenProvider};
use copilot_client::data_cache::{DataCache, namespace};
use copilot_client::emailauth::{ApiBase, CodeSent, EmailFailure, EmailTokenProvider};
use copilot_client::engine::{AudioParts, Engine, UiEvent};
use copilot_client::net::client::{self, Adopt, ClientConfig, ClientStats, ConnectionState, StartParams};
use copilot_client::net::close::StopReason;
use copilot_client::net::seat::{self, SeatHandle, SeatParams};
use copilot_client::oauth::{self, OAuthTokenProvider};
use copilot_client::protocol::{AnswerMode, AnswerStyle, Mode, ScreenImage, ScreenImageMediaType};
use copilot_client::wake::{self, Progress};
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
/// Settings changed outside the current view (startup, another window, or a global hotkey).
const SETTINGS_EVENT: &str = "copilot://settings";
/// The overlay's live comfort prefs (opacity, view, click-through), pushed to the overlay
/// window when a hotkey or a move between monitors changes them.
const OVERLAY_EVENT: &str = "copilot://overlay";
/// Coding mode: the tray's shots changed (a list of `shots::ShotView`), and what went wrong
/// with a hotkey's shot (a message), since a hotkey has no caller to return an error to.
const SHOTS_EVENT: &str = "copilot://coding-shots";
const SHOTS_ERROR_EVENT: &str = "copilot://coding-error";
/// A problem's shots were sent: `SentShots`, for the card of its question.
const SHOTS_SENT_EVENT: &str = "copilot://coding-sent";
/// The overlay asked to open the main window's Settings screen ("All settings…").
const OPEN_SETTINGS_EVENT: &str = "copilot://open-settings";
/// A call ended, or the idle overlay asked to return to the main Call screen.
const OPEN_CALL_EVENT: &str = "copilot://open-call";
/// The on-demand server (wake.rs): {"state": "starting" | "ready" | "failed", "detail"}.
const SERVER_EVENT: &str = "copilot://server";
/// A background call while the VM is stopped: it does not start the VM by itself.
const SERVER_ASLEEP: &str = "The server is asleep; it starts when you use the app.";
const CREDENTIAL_SERVICE: &str = "com.copilot.desktop";
/// The Credential Manager entry older versions kept the recordings' key in.
const OLD_RECORDING_KEY: &str = "me-audio-key";
struct AppState {
    settings: Mutex<Settings>,
    settings_path: PathBuf,
    secrets: Arc<dyn SecretStore>,
    data_cache: DataCache,
    /// The local stack's dev token. Never used in the cloud environment.
    dev_tokens: Arc<DevTokenProvider>,
    /// The deployed stage's browser sign-in (Google or Cognito), made from `settings.oauth`.
    oauth: Mutex<Option<Arc<OAuthTokenProvider>>>,
    /// The deployed stage's email sign-in (emailauth.rs), for the stage in `cloud_api_url`.
    email: Mutex<Option<Arc<EmailTokenProvider>>>,
    /// The deployed stage's Career-OS sign-in (careerossignin.rs), for the same stage.
    career_os: Mutex<Option<Arc<CareerOsTokenProvider>>>,
    /// For the email sign-in's calls to the API, which may have to wake the server first.
    handle: AppHandle,
    engine: Mutex<Option<Arc<Engine>>>,
    /// A guest seat on someone else's call (net/seat.rs); never at the same time as `engine`.
    seat: Mutex<Option<SeatHandle>>,
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
    /// The stage VM's address while it is known to run (its URLs carry `{host}`; wake.rs).
    server_host: Mutex<Option<String>>,
    /// One start at a time; other calls wait for it.
    server_start: tokio::sync::Mutex<()>,
    /// Coding mode's tray: the shots of one problem, waiting for Solve (shots.rs).
    shots: Mutex<Vec<shots::Shot>>,
    /// The shots of the last problems sent, by their question, for the overlay's cards.
    sent_shots: Mutex<std::collections::VecDeque<SentShots>>,
}

/// The shots one screenshot question was asked with (the overlay shows them on its card).
#[derive(Clone, Serialize)]
struct SentShots {
    question_id: Uuid,
    shots: Vec<shots::ShotView>,
}

/// Problems whose shots the overlay can still show (6 shots of up to 375 KB each).
const SENT_SHOTS_KEPT: usize = 10;

#[derive(Default)]
struct OverlayState {
    click_through: AtomicBool,
    geom_gen: AtomicU64,
    shortcuts: Mutex<Vec<(Shortcut, Action)>>,
}

/// The overlay's comfort prefs for the webview: how see-through it is, whether it is compact,
/// whether clicks pass through it, whether it stays on top, and the hotkeys to show on the
/// overlay's own settings menu.
#[derive(Clone, Serialize)]
struct OverlayPrefs {
    opacity: f64,
    compact: bool,
    click_through: bool,
    on_top: bool,
    hotkeys: OverlayHotkeys,
}

/// The overlay's hotkeys as chips on its settings menu (and the key its click-through indicator
/// names). Mirrors the bindings in settings (hotkeys.rs).
#[derive(Clone, Serialize)]
struct OverlayHotkeys {
    toggle_overlay: String,
    click_through: String,
    click_through_off: String,
    opacity_up: String,
    opacity_down: String,
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
        if shutdown::pending(&self.handle) {
            return false;
        }
        let running = self.engine.lock().unwrap().is_some();
        self.gate.claim_install(running)
    }

    /// For start_session: refuse while an update installs, else mark a session as starting
    /// until the returned guard drops.
    fn begin_start(&self) -> Res<Starting<'_>> {
        if shutdown::pending(&self.handle) {
            return Err("Finish ending the current call before starting another.".into());
        }
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

    /// The email sign-in for the configured stage; None without a cloud API URL.
    fn email(&self) -> Option<Arc<EmailTokenProvider>> {
        let stage = self.settings.lock().unwrap().cloud_api_url.trim().to_string();
        if stage.is_empty() {
            return None;
        }
        let mut slot = self.email.lock().unwrap();
        if let Some(p) = slot.as_ref().filter(|p| p.stage() == stage) {
            return Some(p.clone());
        }
        let base = Arc::new(StageApi { app: self.handle.clone() });
        let p = Arc::new(EmailTokenProvider::new(stage, base, self.secrets.clone()));
        *slot = Some(p.clone());
        Some(p)
    }

    /// The email sign-in, if that is how the user is signed in to the stage.
    fn email_signed_in(&self) -> Option<Arc<EmailTokenProvider>> {
        self.email().filter(|p| p.signed_in_as().is_some())
    }

    /// The Career-OS sign-in for the configured stage; None without a cloud API URL.
    fn career_os(&self) -> Option<Arc<CareerOsTokenProvider>> {
        let stage = self.settings.lock().unwrap().cloud_api_url.trim().to_string();
        if stage.is_empty() {
            return None;
        }
        let mut slot = self.career_os.lock().unwrap();
        if let Some(p) = slot.as_ref().filter(|p| p.stage() == stage) {
            return Some(p.clone());
        }
        let base = Arc::new(StageApi { app: self.handle.clone() });
        let p = Arc::new(CareerOsTokenProvider::new(stage, base, self.secrets.clone()));
        *slot = Some(p.clone());
        Some(p)
    }

    /// The Career-OS sign-in, if that is how the user is signed in to the stage.
    fn career_os_signed_in(&self) -> Option<Arc<CareerOsTokenProvider>> {
        self.career_os().filter(|p| p.signed_in_as().is_some())
    }

    /// Who is signed in to the stage, and how: at most one of email, Career-OS, or the browser.
    fn signed_in(&self) -> Option<(String, &'static str)> {
        if let Some(who) = self.email().and_then(|p| p.signed_in_as()) {
            return Some((who, "email"));
        }
        if let Some(who) = self.career_os().and_then(|p| p.signed_in_as()) {
            return Some((who, "career_os"));
        }
        self.oauth().and_then(|p| p.signed_in_as()).map(|who| (who, "browser"))
    }

    /// The access token of the sign-in already on this computer, for proving an account or for
    /// adding Career-OS to it. None when nobody is signed in.
    async fn proof_token(&self) -> Option<String> {
        let provider: Arc<dyn TokenProvider> = if let Some(p) = self.email_signed_in() {
            p
        } else if let Some(p) = self.career_os_signed_in() {
            p
        } else {
            self.oauth().filter(|p| p.signed_in_as().is_some())?
        };
        provider.current().await.ok().map(|t| t.access_token)
    }

    /// Drop every sign-in except `keep` (`email`, `career_os`, or `browser`).
    async fn end_other_sign_ins(&self, keep: &str) {
        self.data_cache.clear();
        if keep != "email"
            && let Some(p) = self.email_signed_in()
        {
            let _ = p.sign_out().await;
        }
        if keep != "career_os"
            && let Some(p) = self.career_os_signed_in()
        {
            let _ = p.sign_out().await;
        }
        if keep != "browser"
            && let Some(p) = self.oauth()
            && p.signed_in_as().is_some()
        {
            let _ = p.sign_out().await;
        }
    }

    /// The token source for the current environment: the dev token locally, the sign-in in the
    /// cloud (an emailed code or the browser, whichever the user signed in with).
    fn tokens(&self) -> Res<Arc<dyn TokenProvider>> {
        let env = self.settings.lock().unwrap().environment;
        match env {
            Environment::Local => Ok(self.dev_tokens.clone()),
            Environment::Cloud => {
                if let Some(p) = self.email_signed_in() {
                    return Ok(p);
                }
                if let Some(p) = self.career_os_signed_in() {
                    return Ok(p);
                }
                match self.oauth() {
                    Some(p) => Ok(p),
                    None => Err("Sign in under Settings first.".into()),
                }
            }
        }
    }

    /// Keep what `GET /v1/auth/config` said: the browser sign-in's settings (when the stage has
    /// one), and whether it signs in by email and by Career-OS too.
    fn keep_auth_config(&self, cfg: &oauth::AuthConfig) {
        let browser = cfg.oauth().ok();
        let email = cfg.email.is_some();
        let career_os = cfg.career_os.is_some();
        self.update_settings(|s| {
            if browser.is_some() {
                s.oauth = browser;
            }
            s.email_sign_in = Some(email);
            s.career_os_sign_in = Some(career_os);
        });
    }

    /// The API client. `wake`: start the stage's VM if it is not known to run (a call the user
    /// made); otherwise a stopped VM fails the call with SERVER_ASLEEP (background polls).
    async fn api(&self, app: &AppHandle, wake: bool) -> Result<ApiClient, String> {
        let context = self.data_cache.context();
        let (url, profile) = {
            let s = self.settings.lock().unwrap();
            (s.active_urls().1.to_string(), s.profile.clone())
        };
        let tokens = self.tokens()?;
        let url = self.fill_url(app, &url, wake).await?;
        if self.data_cache.context() != context {
            return Err("Workspace changed while connecting; try again.".into());
        }
        Ok(ApiClient::new(&url, tokens)
            .map_err(|e| e.to_string())?
            .with_client(self.client.clone())
            .with_profile(profile))
    }

    /// `url` with the VM's address in place of `{host}` (wake.rs); unchanged if it has none.
    async fn fill_url(&self, app: &AppHandle, url: &str, wake: bool) -> Res<String> {
        if !wake::needs_host(url) {
            return Ok(url.to_string());
        }
        Ok(wake::fill(url, &self.server_host(app, wake).await?))
    }

    /// The VM's address: the known one, or (with `wake`) a start through the wake function,
    /// waiting until the API answers. The UI hears each step as SERVER_EVENT.
    async fn server_host(&self, app: &AppHandle, wake: bool) -> Res<String> {
        if let Some(host) = self.server_host.lock().unwrap().clone() {
            return Ok(host);
        }
        if !wake {
            return Err(SERVER_ASLEEP.into());
        }
        let _one = self.server_start.lock().await;
        if let Some(host) = self.server_host.lock().unwrap().clone() {
            return Ok(host);
        }
        let wake_url = release::config()
            .and_then(|r| r.wake_url.clone())
            .ok_or("the server URL needs a host, but this build has no wake URL")?;
        let api_url = self.settings.lock().unwrap().cloud_api_url.trim().to_string();
        let _ = app.emit(SERVER_EVENT, serde_json::json!({ "state": "starting", "detail": null }));
        let progress = {
            let app = app.clone();
            move |p: Progress| {
                let detail = match p {
                    Progress::Vm(status) => format!("the server is {}", status.to_lowercase()),
                    Progress::Services => "the services are starting".to_string(),
                };
                let _ = app.emit(SERVER_EVENT, serde_json::json!({ "state": "starting", "detail": detail }));
            }
        };
        match wake::start(&wake_url, &api_url, progress).await {
            Ok(host) => {
                tracing::info!("server up at {host}");
                *self.server_host.lock().unwrap() = Some(host.clone());
                let _ = app.emit(SERVER_EVENT, serde_json::json!({ "state": "ready", "detail": null }));
                Ok(host)
            }
            Err(e) => {
                let _ = app.emit(SERVER_EVENT, serde_json::json!({ "state": "failed", "detail": e }));
                Err(e)
            }
        }
    }

    /// The VM did not answer: it may have stopped (idle), so the next call asks again.
    fn forget_server(&self) {
        *self.server_host.lock().unwrap() = None;
    }
}

/// The stage's API for the email sign-in: its address now, starting the server if it sleeps.
struct StageApi {
    app: AppHandle,
}

impl ApiBase for StageApi {
    fn api_url(&self) -> BoxFuture<'_, Result<String, String>> {
        Box::pin(async move {
            let state = self.app.state::<AppState>();
            let url = state.settings.lock().unwrap().cloud_api_url.trim().to_string();
            state.fill_url(&self.app, &url, true).await
        })
    }
}

/// An API error for the UI. A 426 (this app is too old for the server) also starts the
/// update at once.
fn api_error(app: &AppHandle, e: ApiError) -> String {
    if let ApiError::Outdated { min_version, .. } = &e {
        outdated(app, &format!("the API needs {min_version} or newer"));
    }
    if let ApiError::Network(_) = &e {
        app.state::<AppState>().forget_server();
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

/// WebView2's flags, the same for every window: windows that share a profile must open with the
/// very same flags, so tauri.conf.json gives the main window this exact string (a test checks).
/// wry's defaults (no mini menu, no SmartScreen, sound without a click), plus: a hidden,
/// minimized or covered window keeps its full speed. The window sharing the screen is often
/// out of sight during a call, and it carries out the controlling guest's input.
const BROWSER_ARGS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
--autoplay-policy=no-user-gesture-required --disable-renderer-backgrounding \
--disable-background-timer-throttling --disable-backgrounding-occluded-windows";

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
    /// A cloud build: no local environment or dev token, the stage's URLs fixed.
    release: Option<ReleaseView>,
    dev_token_set: bool,
    /// The cloud environment's signed-in user (their email), if any.
    signed_in_as: Option<String>,
    /// How they signed in: "email", "career_os", or "browser" (Google or Cognito).
    signed_in_with: Option<&'static str>,
    /// Whether the stage offers sign-in by email; None until the app has asked it.
    email_sign_in: Option<bool>,
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
    let (signed_in_as, signed_in_with) = match state.signed_in() {
        Some((who, how)) => (Some(who), Some(how)),
        None => (None, None),
    };
    let email_sign_in = settings.email_sign_in;
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
        signed_in_with,
        email_sign_in,
        ready,
        warnings,
        updates: UpdatesView { enabled: cfg!(feature = "release"), checked_at: checked_at(&app) },
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
    settings.display_name = stealth::normalize_display_name(&settings.display_name)?;
    let token_changed = dev_token.is_some();
    if let Some(token) = dev_token {
        state.dev_tokens.save(&token).map_err(|e| e.to_string())?;
    }
    let kept = state.settings.lock().unwrap().clone();
    let context_changed = token_changed
        || settings.environment != kept.environment
        || settings.active_urls().1 != kept.active_urls().1;
    if context_changed {
        state.data_cache.clear();
        state.forget_server();
    }
    // The sign-in settings belong to the stage they came from: a new cloud API URL needs a new
    // sign-in.
    let same_stage = settings.cloud_api_url.trim() == kept.cloud_api_url.trim();
    let oauth = if same_stage { kept.oauth } else { None };
    let email_sign_in = if same_stage { kept.email_sign_in } else { None };
    let career_os_sign_in = if same_stage { kept.career_os_sign_in } else { None };
    // What the app keeps for itself, whatever the screen sent (the per-monitor overlay layouts
    // and the monitor it was last on are the app's, not the screen's).
    state.update_settings(|s| {
        *s = Settings {
            last_session_id: kept.last_session_id,
            profile: kept.profile,
            oauth,
            email_sign_in,
            career_os_sign_in,
            last_version: kept.last_version,
            pending_update: kept.pending_update,
            layouts: kept.layouts,
            overlay_monitor: kept.overlay_monitor,
            overlay_on_top: s.overlay_on_top,
            window_opacity: s.window_opacity,
            coding_mode: kept.coding_mode,
            coding_language: kept.coding_language,
            ..settings.clone()
        }
    });
    if context_changed {
        state.data_cache.clear();
    }
    // A waiting update shows the new mode at once.
    #[cfg(feature = "release")]
    release::updater::refresh_ready(&app);
    // The hotkeys may have been rebound: register the new set.
    register_hotkeys(&app);
    // Reconcile OFF too, including a hidden overlay or an earlier failed native transition.
    // apply() reads the current saved settings, so concurrent saves cannot restore stale ON.
    stealth::apply(&app, settings.stealth, &settings.display_name);
    // A failed file write still stores the name. The next save tries the file again.
    if settings.display_name != kept.display_name || program_name::differs(&settings.display_name) {
        program_name::install(&settings.display_name)?;
    }
    let _ = app.emit(SETTINGS_EVENT, ());
    Ok(())
}

/// The audio test (Settings and the Session tab, before a call; audio::check): the speaking
/// phase, then the tone on the output. The microphone is judged only while the user speaks, so
/// a mic that hears the speakers does not pass on the tone.
const AUDIO_CHECK_SPEAK: Duration = Duration::from_millis(3_000);
const AUDIO_CHECK_AFTER_TONE: Duration = Duration::from_millis(300);
const AUDIO_CHECK_EVENT: &str = "copilot://audio-check";

#[derive(Clone, Serialize)]
struct AudioCheckMeter {
    phase: &'static str,
    #[serde(flatten)]
    meter: check::Meter,
}

/// Opens the chosen microphone and output (loopback) for about five seconds: three for the
/// user to speak, then a short tone on the output. Refused while a session runs or starts, and
/// no session starts meanwhile, since both would open the same devices.
#[tauri::command]
async fn audio_check(
    app: AppHandle,
    state: State<'_, AppState>,
    mic_device: Option<String>,
    output_device: Option<String>,
    part: Option<String>,
) -> Res<check::AudioCheck> {
    let _starting = state.begin_start()?;
    // Settings tests each device on its own row: "mic" only listens, "call" only plays the
    // tone; without a part, both in turn.
    let (speak, play) = match part.as_deref() {
        Some("mic") => (AUDIO_CHECK_SPEAK, false),
        Some("call") => (Duration::ZERO, true),
        _ => (AUDIO_CHECK_SPEAK, true),
    };
    tauri::async_runtime::spawn_blocking(move || {
        run_audio_check(&app, mic_device, output_device, speak, play)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn run_audio_check(
    app: &AppHandle,
    mic: Option<String>,
    output: Option<String>,
    speak: Duration,
    play: bool,
) -> Res<check::AudioCheck> {
    let (tx, rx) = std::sync::mpsc::sync_channel(512);
    let mut source = DeviceSource::new(mic, output.clone());
    source.start(tx).map_err(|e| e.to_string())?;
    let mut result = check::Check::new();
    let started = Instant::now();
    let tone_ms = if play {
        Duration::from_millis(u64::from(check::TONE_MS)) + AUDIO_CHECK_AFTER_TONE
    } else {
        Duration::ZERO
    };
    let mut tone: Option<std::thread::JoinHandle<bool>> = None;
    let mut emitted = Instant::now();
    while started.elapsed() < speak + tone_ms {
        let speaking = started.elapsed() < speak;
        if play && !speaking && tone.is_none() {
            let out = output.clone();
            tone = Some(std::thread::spawn(move || capture::play_tone(out.as_deref()).is_ok()));
        }
        if let Ok(packet) = rx.recv_timeout(Duration::from_millis(20))
            && (speaking || matches!(packet, Packet::Them(_)))
        {
            result.push(&packet);
        }
        if emitted.elapsed() >= Duration::from_millis(100) {
            emitted = Instant::now();
            let phase = if speaking { "speak" } else { "tone" };
            let _ = app.emit(AUDIO_CHECK_EVENT, AudioCheckMeter { phase, meter: result.meter() });
        }
    }
    source.stop();
    let played = tone.is_some_and(|t| t.join().unwrap_or(false));
    Ok(result.result(played))
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
fn open_calendar_authorization(app: AppHandle, url: String) -> Res<()> {
    use tauri_plugin_opener::OpenerExt;
    if !(url.starts_with("https://accounts.google.com/")
        || url.starts_with("https://login.microsoftonline.com/"))
    {
        return Err("Only configured calendar authorization providers may be opened".into());
    }
    app.opener().open_url(url, None::<&str>).map_err(|error| error.to_string())
}

#[tauri::command]
async fn api_call(
    app: AppHandle,
    state: State<'_, AppState>,
    method: String,
    path: String,
    body: Option<Value>,
    refresh: Option<bool>,
    wake: Option<bool>,
) -> Res<Value> {
    // The Session screen polls the live calls every ten seconds, and the invites to someone
    // else's call (guest seats); that alone does not keep or start the VM (copilotctl idle
    // ignores both too).
    let is_read = method.eq_ignore_ascii_case("GET");
    let wake = wake.unwrap_or(!(is_read && (path == "/sessions/live" || path == "/seats")));
    if !is_read {
        state.data_cache.invalidate(&path);
        let result = state.api(&app, wake).await?.call(&method, &path, body.as_ref()).await;
        state.data_cache.invalidate(&path);
        let _ = app.emit("copilot://data-changed", &path);
        return result.map_err(|e| api_error(&app, e));
    }
    let context = state.data_cache.context();
    let (origin, profile) = {
        let settings = state.settings.lock().unwrap();
        (settings.active_urls().1.to_string(), settings.profile.clone())
    };
    // Refreshing an expired email token can itself use the wake function. Background
    // intent must be enforced before asking the token provider, not only before HTTP.
    if !wake && wake::needs_host(&origin) && state.server_host.lock().unwrap().is_none() {
        return Err(SERVER_ASLEEP.into());
    }
    let tokens = state.tokens()?;
    // This checks local credential expiry even for a memory hit; a cache never becomes
    // an alternate sign-in mechanism. Cloud wake happens only inside the miss closure.
    let token = tokens.current().await.map_err(|e| e.to_string())?;
    if state.data_cache.context() != context {
        return Err("Workspace changed while loading.".into());
    }
    let ns = namespace(&origin, &token.access_token, profile.as_deref());
    let native = &*state;
    let handle = &app;
    native
        .data_cache
        .read(&ns, &path, refresh.unwrap_or(false), context, |path, tag| {
            let tokens = tokens.clone();
            let origin = origin.clone();
            let profile = profile.clone();
            async move {
                let url = native.fill_url(handle, &origin, wake).await.map_err(ApiError::Invalid)?;
                if native.data_cache.context() != context {
                    return Err(ApiError::Invalid("Workspace changed while loading.".into()));
                }
                ApiClient::new(&url, tokens)?
                    .with_client(native.client.clone())
                    .with_profile(profile)
                    .get_conditional(&path, tag.as_deref())
                    .await
            }
        })
        .await
        .map_err(|e| api_error(&app, e))
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
    state.data_cache.invalidate("/documents");
    let result =
        state.api(&app, true).await?.upload_document_for(&kind, &file_name, bytes.clone(), &target).await;
    state.data_cache.invalidate("/documents");
    let _ = app.emit("copilot://data-changed", "/documents");
    result.map_err(|e| api_error(&app, e))
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
    state.data_cache.invalidate(&path);
    let result = state.api(&app, true).await?.upload_raw(&path, &file_name, bytes.clone()).await;
    state.data_cache.invalidate(&path);
    let _ = app.emit("copilot://data-changed", &path);
    result.map_err(|e| api_error(&app, e))
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

/// Start the stage's VM now if it is stopped (the "Try again" on the server note); Ok at once
/// when it is known to run or the stage has no on-demand VM.
#[tauri::command]
async fn wake_server(app: AppHandle, state: State<'_, AppState>) -> Res<()> {
    let api_url = state.settings.lock().unwrap().active_urls().1.to_string();
    state.fill_url(&app, &api_url, true).await.map(|_| ())
}

/// The cloud environment's browser sign-in: the stage's sign-in settings from
/// `GET /v1/auth/config`, then OAuth with PKCE in the default browser. Returns the email.
#[tauri::command]
async fn sign_in(app: AppHandle, state: State<'_, AppState>) -> Res<String> {
    let api_url = state.settings.lock().unwrap().cloud_api_url.trim().to_string();
    if api_url.is_empty() {
        return Err("Set the cloud API URL and save first.".into());
    }
    let api_url = state.fill_url(&app, &api_url, true).await?;
    let fetched = oauth::fetch_auth_config(&api_url).await.map_err(|e| e.to_string())?;
    state.keep_auth_config(&fetched);
    let cfg = fetched.oauth().map_err(|e| e.to_string())?;
    state.update_settings(|s| s.oauth = Some(cfg));
    let provider = state.oauth().ok_or("no sign-in settings")?;
    let opener = app.clone();
    let email = provider
        .sign_in(move |url| {
            opener
                .opener()
                .open_url(url, None::<&str>)
                .map_err(|e| format!("could not open the browser: {e}"))
        })
        .await
        .map_err(|e| e.to_string())?;
    // One sign-in at a time: an email or Career-OS sign-in to this stage ends here.
    state.end_other_sign_ins("browser").await;
    Ok(email)
}

/// Email sign-in, step one: mail a code to the address. Starts the stage's server first if it
/// sleeps, as the browser sign-in does.
#[tauri::command]
async fn email_sign_in_start(
    app: AppHandle,
    state: State<'_, AppState>,
    email: String,
) -> Result<CodeSent, EmailFailure> {
    let provider = state
        .email()
        .ok_or_else(|| EmailFailure::new("unavailable", "Set the cloud API URL and save first."))?;
    // Learn whether the stage offers it (and keep its sign-in settings current) before asking.
    let api_url = state.settings.lock().unwrap().cloud_api_url.trim().to_string();
    let api_url = state.fill_url(&app, &api_url, true).await.map_err(|e| EmailFailure::new("network", e))?;
    match oauth::fetch_auth_config(&api_url).await {
        Ok(cfg) => {
            state.keep_auth_config(&cfg);
            if cfg.email.is_none() {
                return Err(EmailFailure::new("not_offered", "This server does not offer email sign-in."));
            }
        }
        Err(e) => return Err(EmailFailure::network(e)),
    }
    provider.start(&email).await
}

/// Email sign-in, step two: the code for the tokens. Returns the address signed in.
#[tauri::command]
async fn email_sign_in_verify(
    state: State<'_, AppState>,
    email: String,
    code: String,
) -> Result<String, EmailFailure> {
    let provider = state
        .email()
        .ok_or_else(|| EmailFailure::new("unavailable", "Set the cloud API URL and save first."))?;
    let before = state.signed_in().map(|(who, _)| who);
    let who = provider.verify(&email, &code).await?;
    // One sign-in at a time: a browser or Career-OS sign-in to this stage ends here.
    state.end_other_sign_ins("email").await;
    if before.as_deref() != Some(who.as_str()) {
        state.update_settings(|s| s.profile = None);
    }
    Ok(who)
}

/// Career-OS sign-in: the browser page on Career-OS, then the code to the API. Returns a
/// sign-in, or a choice (several linked accounts, or an email that matches one).
#[tauri::command]
async fn career_os_sign_in(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<CareerOsOutcome, CareerOsFailure> {
    let provider = state
        .career_os()
        .ok_or_else(|| CareerOsFailure::new("unavailable", "Set the cloud API URL and save first."))?;
    let api_url = state.settings.lock().unwrap().cloud_api_url.trim().to_string();
    let api_url =
        state.fill_url(&app, &api_url, true).await.map_err(|e| CareerOsFailure::new("network", e))?;
    let cfg = match oauth::fetch_auth_config(&api_url).await {
        Ok(cfg) => cfg,
        Err(e) => return Err(CareerOsFailure::new("network", e.to_string())),
    };
    state.keep_auth_config(&cfg);
    let Some(option) = cfg.career_os else {
        return Err(CareerOsFailure::new("not_offered", "This server does not offer HubRuby sign-in."));
    };
    let opener = app.clone();
    let pending = copilot_client::careerossignin::browser_code(&option, move |url| {
        opener.opener().open_url(url, None::<&str>).map_err(|e| format!("could not open the browser: {e}"))
    })
    .await?;
    // After the browser: a sign-in already on this computer adds Career-OS to that account.
    let bearer = state.proof_token().await;
    let before = state.signed_in().map(|(who, _)| who);
    let outcome = provider.exchange(&pending, bearer.as_deref()).await?;
    if outcome.name().is_some() {
        state.end_other_sign_ins("career_os").await;
        if before.as_deref() != outcome.name() {
            state.update_settings(|s| s.profile = None);
        }
    }
    Ok(outcome)
}

/// The person's answer when Career-OS sign-in asked which account, or whether to use an
/// existing one. `existing` uses the sign-in now on this computer as proof.
#[tauri::command]
async fn career_os_complete(
    state: State<'_, AppState>,
    ticket: String,
    choice: String,
    account_id: Option<String>,
) -> Result<CareerOsOutcome, CareerOsFailure> {
    let provider = state
        .career_os()
        .ok_or_else(|| CareerOsFailure::new("unavailable", "Set the cloud API URL and save first."))?;
    let bearer = if choice == "existing" { state.proof_token().await } else { None };
    let before = state.signed_in().map(|(who, _)| who);
    let outcome = provider.complete(&ticket, &choice, account_id.as_deref(), bearer.as_deref()).await?;
    if outcome.name().is_some() {
        state.end_other_sign_ins("career_os").await;
        if before.as_deref() != outcome.name() {
            state.update_settings(|s| s.profile = None);
        }
    }
    Ok(outcome)
}

#[tauri::command]
async fn sign_out(state: State<'_, AppState>) -> Res<()> {
    state.data_cache.clear();
    let result = async {
        if let Some(p) = state.email_signed_in() {
            p.sign_out().await.map_err(|e| e.to_string())?;
        }
        if let Some(p) = state.career_os_signed_in() {
            p.sign_out().await.map_err(|e| e.to_string())?;
        }
        if let Some(p) = state.oauth() {
            p.sign_out().await.map_err(|e| e.to_string())?;
        }
        Ok(())
    }
    .await;
    state.data_cache.clear();
    state.update_settings(|s| s.profile = None);
    result
}

/// The profile switch (ARCHITECTURE.md, "Profiles"): the profile every API call and the next
/// session act for; None is the account itself. Refused while a session runs or starts, so a
/// session never changes profile halfway.
#[tauri::command]
async fn set_profile(app: AppHandle, state: State<'_, AppState>, profile: Option<String>) -> Res<()> {
    let _starting = state.begin_start()?;
    if state.engine.lock().unwrap().is_some() || state.seat.lock().unwrap().is_some() {
        return Err("end the session first".into());
    }
    let profile = profile.map(|p| p.trim().to_string()).filter(|p| !p.is_empty());
    if state.settings.lock().unwrap().profile == profile {
        return Ok(());
    }
    state.data_cache.clear();
    state.update_settings(|s| {
        s.profile = profile;
        s.last_session_id = None;
    });
    state.data_cache.clear();
    let reset = state.history.lock().unwrap().reset_context();
    let _ = app.emit(EVENT, reset);
    Ok(())
}

/// `DELETE /me`. In the cloud the sign-in goes too (on AWS the account deletion also removes
/// the Cognito user); the dev token stays (it is the local gateway's).
#[tauri::command]
async fn delete_account(app: AppHandle, state: State<'_, AppState>) -> Res<Value> {
    state.data_cache.clear();
    let result = state.api(&app, true).await?.delete("/me").await.map_err(|e| api_error(&app, e))?;
    state.data_cache.clear();
    state.update_settings(|s| s.last_session_id = None);
    if state.settings.lock().unwrap().environment == Environment::Cloud {
        if let Some(p) = state.email_signed_in() {
            let _ = p.sign_out().await;
        }
        if let Some(p) = state.oauth() {
            let _ = p.sign_out().await;
        }
    }
    Ok(result)
}

#[derive(Debug, Deserialize)]
struct StartRequest {
    #[serde(default)]
    spoken_language: Option<String>,
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
    /// The Session tab's detailed-answer style; absent from older windows.
    #[serde(default)]
    answer_style: Option<AnswerStyle>,
    /// An online test with no call (coding mode): no audio device is opened.
    #[serde(default)]
    no_call: bool,
}

/// Events are numbered per session (`n`), so a window that replays the snapshot applies only
/// the live events after it.
fn emit(app: &AppHandle, history: &Mutex<History>, event: &UiEvent) {
    let Ok(event) = serde_json::to_value(event) else { return };
    let n = history.lock().unwrap().push(event.clone());
    let _ = app.emit(EVENT, serde_json::json!({ "n": n, "event": event }));
}

/// The server's message when the stage requires a Career-OS link (copilot_core.careeros).
const CAREER_OS_REQUIRED: &str = "Please link your HubRuby account to use this app (Settings → HubRuby).";

#[tauri::command]
async fn start_session(app: AppHandle, state: State<'_, AppState>, request: StartRequest) -> Res<()> {
    launch(app, state, request, None).await
}

/// "Open" on the profile's running call (Session and History, "Live now"; GET /sessions/live):
/// take it over in this app, with this computer's audio, from wherever it was running. The
/// call is the current profile's, so the session URL names it.
#[derive(Debug, Deserialize)]
struct OpenRequest {
    session_id: Uuid,
    mode: Mode,
    platform: String,
    /// The next frame numbers the gateway accepts, them and me.
    next_frame: [u32; 2],
    me_audio: bool,
    /// How long the call has been running, to continue its capture clock.
    elapsed_ms: u64,
    /// An online test with no call: no audio is opened here either.
    #[serde(default)]
    no_call: bool,
}

#[tauri::command]
async fn open_session(app: AppHandle, state: State<'_, AppState>, request: OpenRequest) -> Res<()> {
    let adopt = Adopt {
        session_id: request.session_id,
        next_frame: request.next_frame,
        me_audio: request.me_audio,
        capture_offset_ms: u32::try_from(request.elapsed_ms).unwrap_or(u32::MAX),
    };
    // The start parameters are what the gateway already has; they are only sent if the call
    // is gone, and then the client stops instead (Adopt).
    let start = StartRequest {
        spoken_language: None,
        mode: request.mode,
        job_id: None,
        interview_id: None,
        meeting_title: None,
        employer_id: None,
        agenda: None,
        material_ids: vec![],
        platform: request.platform,
        store_transcript: true,
        answer_style: None,
        no_call: request.no_call,
    };
    launch(app, state, start, Some(adopt)).await
}

/// "End" on the profile's running call: this app's own live call ends as usual; any other is
/// resumed from here and ended, which stops the app still holding it (4410). `profile` is the
/// app's current profile (None: "Me").
#[tauri::command]
async fn end_live_session(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: Uuid,
    profile: Option<String>,
) -> Res<String> {
    state.data_cache.invalidate(&format!("/sessions/{session_id}"));
    let mine = state
        .engine
        .lock()
        .unwrap()
        .clone()
        .filter(|_| state.settings.lock().unwrap().last_session_id == Some(session_id));
    if let Some(engine) = mine {
        engine.request_end(true).await;
        return Ok(format!("ending session {session_id}"));
    }
    let url = {
        let mut s = state.settings.lock().unwrap().clone();
        s.profile = profile;
        s.session_url()
    };
    let url = state.fill_url(&app, &url, true).await?;
    let tokens = state.tokens()?;
    let code = client::end_session(&url, tokens.as_ref(), session_id).await?;
    state.data_cache.invalidate(&format!("/sessions/{session_id}"));
    let _ = app.emit("copilot://data-changed", format!("/sessions/{session_id}"));
    state.update_settings(|s| {
        if s.last_session_id == Some(session_id) {
            s.last_session_id = None;
        }
    });
    Ok(format!("ended session {session_id} (close {code:?})"))
}

async fn launch(
    app: AppHandle,
    state: State<'_, AppState>,
    request: StartRequest,
    adopt: Option<Adopt>,
) -> Res<()> {
    let _starting = state.begin_start()?;
    if state.seat.lock().unwrap().is_some() {
        return Err("Stop assisting before you start your own call.".into());
    }
    let tokens = state.tokens()?;
    tokens.current().await.map_err(|e| e.to_string())?;
    let settings = state.settings.lock().unwrap().clone();
    if settings.environment == Environment::Cloud {
        // A stage that requires a Career-OS link refuses the session anyway; say why first.
        // The check is best effort: when it cannot be made, the gateway decides.
        if let Ok(conn) = state.api(&app, true).await?.get("/career-os/connection").await
            && conn["required"] == Value::Bool(true)
            && conn["connected"] != Value::Bool(true)
        {
            return Err(CAREER_OS_REQUIRED.into());
        }
    }
    state.history.lock().unwrap().clear();

    let vad = || -> Res<ChannelVad> {
        Ok(ChannelVad::new(Box::new(Silero::new().map_err(|e| e.to_string())?), VadParams::default()))
    };
    let aec: Box<dyn EchoCanceller> =
        if settings.echo_cancellation { Box::new(WebRtcAec::new()) } else { Box::new(NoEchoCancellation) };
    // An online test has no call: nothing is opened, nothing is sent.
    let source: Box<dyn AudioSource> = if request.no_call {
        Box::new(SilentSource)
    } else {
        Box::new(DeviceSource::new(settings.mic_device.clone(), settings.output_device.clone()))
    };
    let parts = AudioParts { source, aec, them_vad: vad()?, me_vad: vad()? };
    let session_url = state.fill_url(&app, &settings.session_url(), true).await?;
    let mut config = ClientConfig::new(
        session_url,
        StartParams {
            spoken_language: request.spoken_language,
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
            answer_style: request.answer_style,
            no_call: request.no_call,
        },
    );
    config.adopt = adopt;
    let sink = {
        let app = app.clone();
        let history = state.history.clone();
        Arc::new(move |event: UiEvent| {
            if let UiEvent::Connection { state: ConnectionState::Live { session_id, .. } } = &event {
                let sid = *session_id;
                app.state::<AppState>().data_cache.invalidate(&format!("/sessions/{sid}"));
                let _ = app.emit("copilot://data-changed", format!("/sessions/{sid}"));
                app.state::<AppState>().update_settings(|s| s.last_session_id = Some(sid));
            }
            if matches!(&event, UiEvent::Stopped { .. }) {
                let state = app.state::<AppState>();
                let sid = state.settings.lock().unwrap().last_session_id;
                if let Some(sid) = sid {
                    state.data_cache.invalidate(&format!("/sessions/{sid}"));
                    let _ = app.emit("copilot://data-changed", format!("/sessions/{sid}"));
                }
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

    let showing = engine.clone();
    let show_app = app.clone();
    if let Err(error) = app.run_on_main_thread(move || {
        let current = {
            let state = show_app.state::<AppState>();
            state.engine.lock().unwrap().as_ref().is_some_and(|engine| Arc::ptr_eq(engine, &showing))
        };
        // Check identity after reaching the UI thread: an immediate failure or a newer call
        // may already have replaced this one. No engine lock crosses native window calls.
        if current && let Err(error) = call_window::started(&show_app) {
            tracing::warn!("show live call window: {error}");
        }
    }) {
        tracing::warn!("schedule live call window: {error}");
    }

    // When the client stops (ended, replaced, no minutes, ...), stop the audio and clear up:
    // the socket is closed for good.
    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        engine.finish().await;
        let finished_current = {
            let state = app2.state::<AppState>();
            let finished_current = {
                let mut slot = state.engine.lock().unwrap();
                let current = slot.as_ref().is_some_and(|e| Arc::ptr_eq(e, &engine));
                if current {
                    *slot = None;
                }
                current
            };
            let now = state.clock();
            state.activity.lock().unwrap().last_session_end = Some(now);
            finished_current
        };
        if finished_current {
            let restore_app = app2.clone();
            if let Err(error) = app2.run_on_main_thread(move || {
                let active = {
                    let state = restore_app.state::<AppState>();
                    state.engine.lock().unwrap().is_some() || state.seat.lock().unwrap().is_some()
                };
                if !active && let Err(error) = call_window::stopped(&restore_app) {
                    tracing::warn!("restore main call window: {error}");
                }
            }) {
                tracing::warn!("schedule main call window: {error}");
            }
        }
    });
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
async fn end_other_session(app: AppHandle, state: State<'_, AppState>) -> Res<String> {
    let (url, sid) = {
        let s = state.settings.lock().unwrap();
        (s.session_url(), s.last_session_id)
    };
    let url = state.fill_url(&app, &url, true).await?;
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

/// Coding mode: a shot of the problem's window (shots.rs), on a worker thread: capturing and
/// encoding take 100 to 400 ms. With stealth off, the overlay is kept out of a monitor grab
/// (a window grab never shows it). The current Stealth choice is restored after the grab.
async fn take_shot(app: &AppHandle) -> Res<shots::Shot> {
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let exclusion = std::sync::Mutex::new(None);
        let keep_out = |on: bool| {
            if on {
                let mut guard = exclusion.lock().unwrap();
                if guard.is_none() {
                    *guard = Some(stealth::protect_screenshot_overlay(&app));
                    // The affinity is set on the window's own thread; let it land first.
                    std::thread::sleep(Duration::from_millis(150));
                }
            } else {
                exclusion.lock().unwrap().take();
            }
        };
        let shot = shots::grab(std::process::id(), &keep_out)?;
        tracing::info!(
            "coding shot {}: {:?}, {}x{}, {} KB, capture {} ms, encode {} ms",
            shot.id,
            shot.source,
            shot.width,
            shot.height,
            shot.bytes() / 1024,
            shot.capture_ms,
            shot.encode_ms
        );
        Ok(shot)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn emit_shots(app: &AppHandle) {
    let views: Vec<shots::ShotView> =
        app.state::<AppState>().shots.lock().unwrap().iter().map(|s| s.view()).collect();
    let _ = app.emit(SHOTS_EVENT, views);
}

fn tray_full(app: &AppHandle) -> Res<()> {
    if app.state::<AppState>().shots.lock().unwrap().len() >= shots::MAX_SHOTS {
        return Err(format!("{} shots at most: solve these, or remove one", shots::MAX_SHOTS));
    }
    Ok(())
}

/// The Add shot hotkey and button: one more shot of this problem into the tray.
async fn add_shot(app: &AppHandle) -> Res<()> {
    tray_full(app)?;
    let shot = take_shot(app).await?;
    tray_full(app)?;
    app.state::<AppState>().shots.lock().unwrap().push(shot);
    emit_shots(app);
    Ok(())
}

/// The Solve hotkey and button: the tray's shots (or, with none, one taken now) sent as one
/// problem, in the overlay's answer mode and language. The tray empties once they are sent.
async fn solve(app: &AppHandle) -> Res<()> {
    let state = app.state::<AppState>();
    let engine = state
        .engine()
        .map_err(|_| "Start a session first: shots are answered in a running session".to_string())?;
    let mut shots: Vec<shots::Shot> = state.shots.lock().unwrap().clone();
    if shots.is_empty() {
        shots.push(take_shot(app).await?);
    }
    let (mode, language) = {
        let s = state.settings.lock().unwrap();
        (s.coding_mode.clone(), s.coding_language.clone())
    };
    let answer_mode = if mode == "assessment" { AnswerMode::Assessment } else { AnswerMode::Interview };
    let images = shots
        .iter()
        .map(|s| ScreenImage {
            data: s.data.clone(),
            media_type: match s.media_type {
                "image/jpeg" => ScreenImageMediaType::ImageJpeg,
                "image/png" => ScreenImageMediaType::ImagePng,
                _ => ScreenImageMediaType::ImageWebp,
            },
            width: u64::from(s.width),
            height: u64::from(s.height),
            title: s.title.as_ref().map(|t| t.chars().take(300).collect()),
        })
        .collect();
    let sent: Vec<u32> = shots.iter().map(|s| s.id).collect();
    // The question's id is ours, so its card can show the shots it was asked with.
    let question_id = Uuid::now_v7();
    let record = SentShots { question_id, shots: shots.iter().map(|s| s.view()).collect() };
    {
        let mut kept = state.sent_shots.lock().unwrap();
        kept.push_back(record.clone());
        while kept.len() > SENT_SHOTS_KEPT {
            kept.pop_front();
        }
    }
    let _ = app.emit(SHOTS_SENT_EVENT, record);
    engine.screenshot(question_id, images, answer_mode, Some(language)).await;
    // Shots added while this one was being taken stay for the next problem.
    state.shots.lock().unwrap().retain(|s| !sent.contains(&s.id));
    emit_shots(app);
    Ok(())
}

#[tauri::command]
async fn coding_add_shot(app: AppHandle) -> Res<()> {
    add_shot(&app).await
}

#[tauri::command]
async fn coding_solve(app: AppHandle) -> Res<()> {
    solve(&app).await
}

#[tauri::command]
fn coding_remove_shot(app: AppHandle, state: State<'_, AppState>, id: u32) {
    state.shots.lock().unwrap().retain(|s| s.id != id);
    emit_shots(&app);
}

/// The shots of the last problems sent, for cards drawn after a reload.
#[tauri::command]
fn coding_sent_shots(state: State<'_, AppState>) -> Vec<SentShots> {
    state.sent_shots.lock().unwrap().iter().cloned().collect()
}

#[tauri::command]
fn coding_shots(state: State<'_, AppState>) -> Vec<shots::ShotView> {
    state.shots.lock().unwrap().iter().map(|s| s.view()).collect()
}

/// The overlay's answer mode ("interview" or "assessment") and language ("" lets the screen
/// decide), kept across calls.
#[tauri::command]
fn set_coding_prefs(state: State<'_, AppState>, mode: String, language: String) {
    let mode = if mode == "assessment" { mode } else { "interview".to_string() };
    let language: String = language.trim().chars().take(40).collect();
    state.update_settings(|s| {
        s.coding_mode = mode;
        s.coding_language = language;
    });
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

/// Change a live call's language without replacing the capture session.
#[tauri::command]
async fn set_spoken_language(state: State<'_, AppState>, language: String, request_id: Uuid) -> Res<()> {
    state.engine()?.set_spoken_language(language, request_id).await;
    Ok(())
}

/// The Session tab's detailed-answer style, changed during a call.
#[tauri::command]
async fn set_answer_style(state: State<'_, AppState>, style: AnswerStyle) -> Res<()> {
    state.engine()?.set_answer_style(style).await;
    Ok(())
}

/// The session's events so far (compacted), whether a session is running, and the number of
/// the next event: a window applies these, then live events from that number on.
#[tauri::command]
fn snapshot(state: State<'_, AppState>) -> (Vec<serde_json::Value>, bool, u64) {
    let (events, next) = state.history.lock().unwrap().snapshot();
    let running = state.engine.lock().unwrap().is_some() || state.seat.lock().unwrap().is_some();
    (events, running, next)
}

/// Join someone else's live call as a guest (ARCHITECTURE.md, "Guest seats"), once the host
/// admitted this account (`GET /seats`). The overlay shows the call read only, from the
/// gateway's `/v1/seat`; nothing of this app's audio is used. The assistant's own Stealth
/// setting controls capture visibility, exactly as it does while hosting a call.
#[derive(Debug, Deserialize)]
struct SeatJoinRequest {
    session_id: Uuid,
    seat_id: Uuid,
}

#[tauri::command]
async fn seat_join(app: AppHandle, state: State<'_, AppState>, request: SeatJoinRequest) -> Res<()> {
    if state.engine.lock().unwrap().is_some() {
        return Err("End your own call before you join someone else's.".into());
    }
    if let Some(old) = state.seat.lock().unwrap().take() {
        old.leave();
    }
    let tokens = state.tokens()?;
    tokens.current().await.map_err(|e| e.to_string())?;
    let settings = state.settings.lock().unwrap().clone();
    let url = state.fill_url(&app, &settings.seat_url(), true).await?;
    state.history.lock().unwrap().clear();
    let sink = {
        let app = app.clone();
        let history = state.history.clone();
        Arc::new(move |event: UiEvent| emit(&app, &history, &event))
    };
    let params = SeatParams {
        url,
        session_id: request.session_id,
        seat_id: request.seat_id,
        client_version: state.client.clone(),
    };
    let (handle, task) = seat::spawn(params, tokens, sink);
    *state.seat.lock().unwrap() = Some(handle.clone());
    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        let _ = task.await;
        let state = app2.state::<AppState>();
        {
            let mut slot = state.seat.lock().unwrap();
            if slot.as_ref().is_some_and(|s| s.seat_id == handle.seat_id) {
                *slot = None;
            }
        }
    });
    if let Some(overlay) = app.get_webview_window("overlay") {
        let _ = stealth::show(&overlay);
        stealth::refresh(&app);
    }
    Ok(())
}

/// Stop watching: the seat stays this account's until the host removes it or the call ends.
/// The overlay goes away with the call it showed.
#[tauri::command]
fn seat_leave(app: AppHandle, state: State<'_, AppState>) {
    if let Some(seat) = state.seat.lock().unwrap().take() {
        seat.leave();
    }
    if let Some(overlay) = app.get_webview_window("overlay") {
        let _ = overlay.hide();
    }
}

/// A note for the host of the call this app watches; returns its id (its result names it).
#[tauri::command]
fn seat_note(state: State<'_, AppState>, text: String) -> Res<Uuid> {
    let text = text.trim().to_string();
    if text.is_empty() || text.chars().count() > 1000 {
        return Err("A note is 1 to 1,000 characters.".into());
    }
    let seat = state.seat.lock().unwrap().clone().ok_or("not watching a call")?;
    Ok(seat.note(text))
}

/// Host: Ask AI, on a guest's note. The note's words become a typed question.
#[tauri::command]
async fn ask_note(state: State<'_, AppState>, note_id: Uuid, text: String) -> Res<()> {
    state.engine()?.ask_note(note_id, text).await;
    Ok(())
}

/// Host: pause (false) or resume what the call's guests see.
#[tauri::command]
async fn set_sharing(state: State<'_, AppState>, on: bool) -> Res<()> {
    state.engine()?.set_sharing(on).await;
    Ok(())
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
    if show {
        bring_back_overlay(&w);
        // The overlay is created hidden. Set exclusion again once it is on screen, in case the
        // call at startup did not stick, and give it the same name as the main window.
        let (on, name) = {
            let state = app.state::<AppState>();
            let settings = state.settings.lock().unwrap();
            (settings.stealth, settings.display_name.clone())
        };
        stealth::apply(&app, on, &name);
        Ok(())
    } else {
        w.hide().map_err(|e| e.to_string())
    }
}

/// Minimize keeps the normal taskbar entry; Stealth uses the show hotkey to restore.
#[tauri::command]
fn minimize_overlay(app: AppHandle) -> Res<()> {
    let w = overlay_window(&app).ok_or("no overlay window")?;
    w.minimize().map_err(|e| e.to_string())
}
/// Show the overlay, restoring it if it was minimized, and focus it.
fn bring_back_overlay(w: &WebviewWindow) {
    let _ = stealth::show(w);
    if matches!(w.is_minimized(), Ok(true)) {
        let _ = w.unminimize();
    }
    let _ = w.set_focus();
}

/// Register every bound hotkey from settings and remember which action each fired key drives,
/// so the handler can dispatch. Invalid or unbound accelerators are skipped (the settings
/// screen flags them); the click-through off switch always keeps a working key (hotkeys.rs).
fn register_hotkeys(app: &AppHandle) {
    if let Err(error) = workspace_window::clear_click_through(app) {
        tracing::warn!("workspace click-through recovery before rebinding: {error}");
        return;
    }
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
        Action::ToggleStealth => toggle_stealth(app),
        Action::ClickThrough => {
            let on = !app.state::<AppState>().overlay.click_through.load(Ordering::SeqCst);
            if let Err(error) = apply_click_through(app, on) {
                tracing::warn!("click-through: {error}");
            }
        }
        Action::ClickThroughOff => {
            if let Err(error) = apply_click_through(app, false) {
                tracing::warn!("workspace click-through recovery: {error}");
            }
        }
        Action::OpacityUp => nudge_opacity(app, 1),
        Action::OpacityDown => nudge_opacity(app, -1),
        Action::AddShot | Action::Solve => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                let done = if action == Action::AddShot { add_shot(&app).await } else { solve(&app).await };
                if let Err(e) = done {
                    let _ = app.emit(SHOTS_ERROR_EVENT, e);
                }
            });
        }
        Action::StopControl => {
            // A guest controlling the screen stops at once, whatever the windows do.
            remote::end();
            let _ = app.emit("remote:stopped", ());
        }
    }
}

/// Flip the live saved value under the settings lock. No session or visible window is needed,
/// and the verified native path also clears exclusion on a hidden overlay.
fn toggle_stealth(app: &AppHandle) {
    let state = app.state::<AppState>();
    state.update_settings(|settings| settings.stealth = !settings.stealth);
    let (on, name) = {
        let settings = state.settings.lock().unwrap();
        (settings.stealth, settings.display_name.clone())
    };
    stealth::apply(app, on, &name);
    let _ = app.emit(SETTINGS_EVENT, ());
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

/// Both windows read the same appearance and interaction preferences; only compact view
/// belongs to the overlay. Geometry and monitor changes cannot override appearance.
fn build_prefs(app: &AppHandle, compact: bool) -> OverlayPrefs {
    let state = app.state::<AppState>();
    let click_through = state.overlay.click_through.load(Ordering::SeqCst);
    let (opacity, on_top, hotkeys) = {
        let s = state.settings.lock().unwrap();
        (
            s.window_opacity(),
            s.overlay_on_top,
            OverlayHotkeys {
                toggle_overlay: s.toggle_overlay_hotkey.trim().to_string(),
                click_through: s.click_through_hotkey.trim().to_string(),
                click_through_off: hotkeys::click_through_off(&s),
                opacity_up: s.opacity_up_hotkey.trim().to_string(),
                opacity_down: s.opacity_down_hotkey.trim().to_string(),
            },
        )
    };
    OverlayPrefs { opacity, compact, click_through, on_top, hotkeys }
}

fn emit_prefs(app: &AppHandle, l: MonitorLayout) {
    let _ = app.emit_to("overlay", OVERLAY_EVENT, build_prefs(app, l.compact));
}

fn toggle_overlay(app: &AppHandle) {
    let Some(w) = overlay_window(app) else { return };
    // A minimized overlay counts as visible, but the hotkey should bring it back, not hide it.
    if matches!(w.is_visible(), Ok(true)) && !matches!(w.is_minimized(), Ok(true)) {
        let _ = w.hide();
    } else {
        bring_back_overlay(&w);
    }
}

/// Notify both renderers, including a hidden window waiting for the next call.
fn emit_window_prefs(app: &AppHandle) {
    let compact = current_layout(app).is_some_and(|(_, layout)| layout.compact);
    let _ = app.emit_to("overlay", OVERLAY_EVENT, build_prefs(app, compact));
    if let Some(main) = app.get_webview_window("main") {
        let _ = workspace_window::emit_prefs(&main);
    }
}

/// One click-through preference, with a temporary exception for main-window consent.
fn apply_click_through(app: &AppHandle, on: bool) -> Res<()> {
    if on && workspace_window::recovery_shortcut(app).is_none() {
        return Err(
            "Click-through needs a working recovery shortcut. Set a Click-through off key in Settings first."
                .into(),
        );
    }
    let previous = app.state::<AppState>().overlay.click_through.load(Ordering::SeqCst);
    workspace_window::update_both(previous, on, on, |label, value| {
        let Some(window) = app.get_webview_window(label) else { return Ok(()) };
        if label == "main" {
            workspace_window::apply_click_through(&window, value)
        } else {
            window.set_ignore_cursor_events(value).map_err(|error| error.to_string())
        }
    })?;
    app.state::<AppState>().overlay.click_through.store(on, Ordering::SeqCst);
    emit_window_prefs(app);
    Ok(())
}

fn nudge_opacity(app: &AppHandle, steps: i32) {
    app.state::<AppState>().update_settings(|settings| {
        settings.window_opacity = Some(layout::step_opacity(settings.window_opacity(), steps));
    });
    emit_window_prefs(app);
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
/// remembered view. Background opacity is shared and never changes when switching monitors.
fn remember_geometry(app: &AppHandle) {
    let Some(w) = overlay_window(app) else { return };
    // Minimized, Windows parks the window off screen: not a place to remember.
    if matches!(w.is_minimized(), Ok(true)) {
        return;
    }
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
fn overlay_prefs(app: AppHandle) -> OverlayPrefs {
    let compact = current_layout(&app).is_some_and(|(_, layout)| layout.compact);
    build_prefs(&app, compact)
}

/// Both window sliders and the opacity hotkeys update this shared preference.
#[tauri::command]
fn set_overlay_opacity(app: AppHandle, value: f64) {
    app.state::<AppState>()
        .update_settings(|settings| settings.window_opacity = Some(layout::clamp_opacity(value)));
    emit_window_prefs(&app);
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
fn set_click_through(app: AppHandle, on: bool) -> Res<()> {
    apply_click_through(&app, on)
}

/// Both windows stay above other applications, remembered between runs.
#[tauri::command]
fn set_overlay_on_top(app: AppHandle, on: bool) -> Res<()> {
    let previous = app.state::<AppState>().settings.lock().unwrap().overlay_on_top;
    workspace_window::update_both(previous, on, true, |label, value| {
        let Some(window) = app.get_webview_window(label) else { return Ok(()) };
        if label == "main" {
            workspace_window::apply_on_top(&window, value)
        } else {
            window.set_always_on_top(value).map_err(|error| error.to_string())
        }
    })?;
    app.state::<AppState>().update_settings(|s| s.overlay_on_top = on);
    emit_window_prefs(&app);
    Ok(())
}
/// The overlay's "All settings…": bring the main window forward on its Settings screen.
#[tauri::command]
fn open_main_settings(app: AppHandle) -> Res<()> {
    let w = app.get_webview_window("main").ok_or("no main window")?;
    stealth::show(&w)?;
    let _ = w.unminimize();
    let _ = w.set_focus();
    let _ = app.emit_to("main", OPEN_SETTINGS_EVENT, ());
    Ok(())
}

/// The idle overlay's power button returns to setup. During a live call, opening the main
/// screen leaves its overlay in place; only an actual terminal stop hides live controls.
#[tauri::command]
async fn open_main_call(app: AppHandle) -> Res<()> {
    let (send, receive) = tokio::sync::oneshot::channel();
    let main_app = app.clone();
    app.run_on_main_thread(move || {
        let active = {
            let state = main_app.state::<AppState>();
            state.engine.lock().unwrap().is_some() || state.seat.lock().unwrap().is_some()
        };
        let result = if active { call_window::show_main(&main_app) } else { call_window::stopped(&main_app) };
        let _ = send.send(result);
    })
    .map_err(|error| error.to_string())?;
    receive.await.map_err(|_| "main window operation was interrupted".to_string())?
}

/// The app's log file, emptied once it passes 2 MB so it never grows without bound.
/// `None` off Windows or when the folder cannot be written; logs then go to stderr.
fn open_log_file() -> Option<std::fs::File> {
    let dir =
        std::path::PathBuf::from(std::env::var_os("LOCALAPPDATA")?).join("com.copilot.desktop").join("logs");
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join("desktop.log");
    let too_big = std::fs::metadata(&path).is_ok_and(|m| m.len() > 2 * 1024 * 1024);
    std::fs::OpenOptions::new().create(true).append(!too_big).write(true).truncate(too_big).open(path).ok()
}

/// The overlay's "Quit": close the whole app.
#[tauri::command]
async fn quit_app(app: AppHandle) -> Res<()> {
    shutdown::quit(&app).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let filter = || tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into());
    // The installed app has no console, so its warnings go to a file a teammate can send:
    // %LOCALAPPDATA%\com.copilot.desktop\logs\desktop.log, beside updates.log.
    match open_log_file() {
        Some(file) => tracing_subscriber::fmt()
            .with_env_filter(filter())
            .with_ansi(false)
            .with_writer(std::sync::Mutex::new(file))
            .init(),
        None => tracing_subscriber::fmt().with_env_filter(filter()).init(),
    }
    tracing::info!("starting on Windows build {}", stealth::build_number());
    // One app per computer (2026-10-29: two copies on one computer ran two sessions on the
    // same microphone and speakers, under two profiles). A second launch brings the running
    // app's window forward instead.
    let builder = tauri::Builder::default().plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
        background::second_instance(app, &args);
    }));
    #[cfg(feature = "release")]
    let builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
    builder
        // Closing the main window quits the app. Otherwise the hidden overlay window kept the
        // process alive with no window to show, and a second launch had nothing to bring forward.
        .on_window_event(|window, event| {
            if background::on_window_event(window, event) {
                return;
            }
            compact_window::on_window_event(window, event);
            if window.label() == "main" && matches!(event, WindowEvent::Destroyed) {
                // A guest's control ends with the app: nothing they held stays pressed.
                remote::end();
                window.app_handle().exit(0);
            }
            // Re-read native affinity after old Windows reattaches WebView2 on focus.
            // Reconcile OFF as well; only bounded screenshot captures temporarily override it.
            if matches!(event, WindowEvent::Focused(true)) && stealth::capture_needs_repeat() {
                stealth::refresh(window.app_handle());
            }
        })
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
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
            // Also once after an update from a version without email sign-in.
            let needs_auth_config = release::config().is_some()
                && (settings.oauth.is_none()
                    || settings.email_sign_in.is_none()
                    || settings.career_os_sign_in.is_none());
            let overlay_on_top = settings.overlay_on_top;
            let secrets: Arc<dyn SecretStore> = Arc::new(OsSecretStore::new(CREDENTIAL_SERVICE));
            // Versions before live transcription recorded the user's audio for an upload after
            // the call; the gateway no longer takes those, so what is left goes, with its key.
            let leftover = app.path().app_local_data_dir()?.join("me-audio");
            if leftover.exists() {
                let _ = std::fs::remove_dir_all(&leftover);
                let _ = secrets.delete(OLD_RECORDING_KEY);
            }
            app.manage(workspace_window::WorkspaceWindowState::default());
            let data_cache = DataCache::new(app.path().app_cache_dir()?.join("data-v1"), secrets.as_ref());
            app.manage(AppState {
                settings: Mutex::new(settings),
                settings_path,
                dev_tokens: Arc::new(DevTokenProvider::new(secrets.clone())),
                oauth: Mutex::new(None),
                email: Mutex::new(None),
                career_os: Mutex::new(None),
                handle: app.handle().clone(),
                secrets,
                data_cache,
                engine: Mutex::new(None),
                seat: Mutex::new(None),
                history: Arc::new(Mutex::new(History::default())),
                client: format!("desktop-{version}"),
                started: Instant::now(),
                activity: Mutex::new(Activity::default()),
                gate: Gate::default(),
                overlay: OverlayState::default(),
                server_host: Mutex::new(None),
                server_start: tokio::sync::Mutex::new(()),
                shots: Mutex::new(Vec::new()),
                sent_shots: Mutex::new(std::collections::VecDeque::new()),
            });
            #[cfg(feature = "release")]
            app.manage(release::updater::Updates::new(journal, updated.then(|| version.clone())));
            #[cfg(not(feature = "release"))]
            let _ = (updated, journal);
            app.manage(shutdown::QuitState::default());
            tray_menu::setup(app.handle());
            background::setup(app.handle())?;
            register_hotkeys(app.handle());
            // Passive startup checks never wake the VM. Foreground data reads and sign-in
            // still wake it when they need an online response.
            if !background::background_start_requested() {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    // A release build learns its stage's sign-in settings at once, so a sign-in kept
                    // in the Credential Manager (from an earlier install) works without a new one.
                    if needs_auth_config {
                        let state = handle.state::<AppState>();
                        let api_url = state.settings.lock().unwrap().cloud_api_url.clone();
                        let fetched = match state.fill_url(&handle, &api_url, false).await {
                            Ok(api_url) => oauth::fetch_auth_config(&api_url).await,
                            Err(e) => Err(copilot_client::auth::AuthError::NotSignedIn(e)),
                        };
                        match fetched {
                            Ok(cfg) => {
                                state.keep_auth_config(&cfg);
                                // The settings screen may have loaded before this answered.
                                let _ = handle.emit(SETTINGS_EVENT, ());
                            }
                            Err(e) => tracing::info!("sign-in settings not fetched at startup: {e}"),
                        }
                    }
                    if let Ok(api) = handle.state::<AppState>().api(&handle, false).await
                        && let Err(e) = api.get("/me").await
                    {
                        tracing::info!("GET /me at startup: {}", api_error(&handle, e));
                    }
                });
            }
            // Transparent so the opacity setting can make the overlay see-through to the window
            // underneath (the panel paints its own translucent background in styles.css).
            let overlay =
                WebviewWindowBuilder::new(app, "overlay", WebviewUrl::App("index.html#overlay".into()))
                    .title("CoRuby overlay")
                    .inner_size(layout::DEFAULT_W as f64, layout::DEFAULT_H as f64)
                    .min_inner_size(320.0, 240.0)
                    .always_on_top(overlay_on_top)
                    .decorations(false)
                    .skip_taskbar(true)
                    .transparent(true)
                    .visible(false)
                    .additional_browser_args(BROWSER_ARGS)
                    .background_throttling(tauri::utils::config::BackgroundThrottlingPolicy::Disabled)
                    .build()?;
            if let Some(main) = app.get_webview_window("main") {
                workspace_window::apply_on_top(&main, overlay_on_top)?;
            }
            window_drag::setup(app.handle())?;
            window_visibility::setup(app.handle())?;
            // Save the overlay's frame per monitor as it is dragged or resized (debounced).
            let handle = app.handle().clone();
            overlay.on_window_event(move |event| {
                if matches!(event, WindowEvent::Moved(_) | WindowEvent::Resized(_)) {
                    schedule_remember(&handle);
                }
            });
            // Reopen where it was last left, recovering onto a monitor that still exists.
            place_overlay(app.handle());
            // Guest seats' voice runs in the main window (src/voice.ts): its microphone,
            // asked for only while a guest talks, needs no WebView2 prompt.
            if let Some(main) = app.get_webview_window("main") {
                voice::allow_microphone(&main);
                if let Err(error) = workspace_window::fit_initial_window(&main) {
                    tracing::warn!("Initial workspace size: {error}");
                }
            }
            // Both windows exist now. A saved stealth choice excludes them from capture.
            // The overlay stays hidden here, so applying stealth does not paint it.
            program_name::cleanup_previous();
            let (on, name) = {
                let state = app.state::<AppState>();
                let settings = state.settings.lock().unwrap();
                (settings.stealth, settings.display_name.clone())
            };
            let name = stealth::normalize_display_name(&name)
                .unwrap_or_else(|_| stealth::DEFAULT_DISPLAY_NAME.to_string());
            tracing::info!(
                "version {}, stealth {}",
                app.package_info().version,
                if on { "on" } else { "off" }
            );
            stealth::apply(app.handle(), on, &name);
            // Size the initially hidden workspace before presenting it. Background launches
            // remain hidden; normal launches use the existing capture-aware show path.
            if !background::background_start_requested()
                && let Some(main) = app.get_webview_window("main")
                && let Err(error) = stealth::show(&main)
            {
                tracing::warn!("Show workspace: {error}");
            }
            // Task Manager reads the program file, not the window title. Write the saved name
            // now; it shows up the next time this program starts.
            if let Err(e) = program_name::install(&name) {
                tracing::warn!("task manager name: {e}");
            }
            #[cfg(feature = "release")]
            release::updater::start(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            open_calendar_authorization,
            tray_menu::tray_menu_ready,
            tray_menu::tray_menu_action,
            background::background_status,
            background::background_detection_status,
            background::background_hushed_devices,
            background::background_hushed_connect,
            background::background_suggestion,
            background::background_respond,
            background::background_take_accepted,
            background::background_request_permissions,
            background::background_native_activity,
            background::background_set_preferences,
            compact_window::set_compact_window_shape,
            compact_window::resize_compact_window,
            window_drag::start_window_drag,
            window_drag::start_window_resize,
            workspace_window::window_prefs,
            workspace_window::set_window_opacity,
            workspace_window::set_window_on_top,
            workspace_window::set_window_click_through,
            workspace_window::screen_control_prompt,
            get_settings,
            save_settings,
            stealth::capture_exclusion_status,
            list_devices,
            audio_check,
            api_call,
            wake_server,
            upload_document,
            upload_company_file,
            delete_account,
            sign_in,
            email_sign_in_start,
            email_sign_in_verify,
            career_os_sign_in,
            career_os_complete,
            sign_out,
            set_profile,
            start_session,
            end_session,
            end_other_session,
            open_session,
            end_live_session,
            force_question,
            coding_add_shot,
            coding_solve,
            coding_remove_shot,
            coding_shots,
            coding_sent_shots,
            set_coding_prefs,
            respond,
            expand_answer,
            send_feedback,
            set_mode,
            set_answer_style,
            set_spoken_language,
            snapshot,
            live_status,
            seat_join,
            seat_leave,
            seat_note,
            ask_note,
            set_sharing,
            voice_devices,
            native_screen::native_screen_sources,
            native_screen::native_screen_start,
            native_screen::native_screen_frame,
            native_screen::native_screen_stop,
            native_screen::native_screen_quality,
            remote_monitors,
            remote_begin,
            remote_input,
            remote_end,
            show_overlay,
            minimize_overlay,
            overlay_prefs,
            set_overlay_opacity,
            set_overlay_view,
            set_click_through,
            set_overlay_on_top,
            open_main_settings,
            open_main_call,
            quit_app,
            update_status,
            install_update,
            check_updates,
            note_activity,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

// --- Guest seats' voice (voice.rs; the audio itself is src/voice.ts) ---------------------------

/// The output devices, each with what Windows says it is and whether it is the call's own.
#[tauri::command]
async fn voice_devices(state: State<'_, AppState>) -> Res<Vec<voice::VoiceDevice>> {
    let call = state.settings.lock().unwrap().output_device.clone();
    tauri::async_runtime::spawn_blocking(move || voice::devices(call.as_deref()))
        .await
        .map_err(|e| e.to_string())
}

// --- Guest seats' remote input (remote.rs; the decisions are in src/screen.ts) ---------------

/// The monitors, to match the shared picture to one.
#[tauri::command]
fn remote_monitors() -> Vec<remote::Monitor> {
    remote::monitors()
}

/// A guest begins controlling the shared monitor `target`.
#[tauri::command]
fn remote_begin(target: remote::Rect) -> Res<()> {
    remote::begin(target)
}

/// The controlling guest's events, carried out on that monitor. Err when no one controls.
/// Async so it runs on a worker thread: a plain command waits its turn on the UI thread,
/// behind the windows' own work, which the guest feels as lag. SendInput works from any thread.
#[tauri::command]
async fn remote_input(events: Vec<remote::Input>) -> Res<()> {
    remote::input(&events)
}

/// Control ends: everything the guest held is let go.
#[tauri::command]
fn remote_end() {
    remote::end();
}

#[cfg(test)]
mod tests {
    use super::percent_decode;

    #[test]
    fn every_window_opens_webview2_with_the_same_flags() {
        let conf: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let main = &conf["app"]["windows"][0];
        assert_eq!(main["label"], "main");
        assert_eq!(main["additionalBrowserArgs"], super::BROWSER_ARGS);
    }

    #[test]
    fn file_names_are_uri_decoded() {
        assert_eq!(percent_decode("R%C3%A9sum%C3%A9%20Alex.pdf"), "Résumé Alex.pdf");
        assert_eq!(percent_decode("plain.md"), "plain.md");
        assert_eq!(percent_decode("bad%zz.txt"), "bad%zz.txt");
    }
}
