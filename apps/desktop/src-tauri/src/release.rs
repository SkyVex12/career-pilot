//! Release builds (the `release` feature): the installer teammates use. The stage's URLs come
//! from apps/desktop/release.json, embedded at build time (build.rs); the local environment and
//! the dev token are gone, and the app updates itself (the Tauri updater, whose endpoint and
//! public key are in the generated tauri.release.conf.json, from the same file).
//!
//! Updates (README.md, "Installing the desktop app"): the app checks at start and every four
//! hours, downloads a newer version in the background and verifies its signature, and installs
//! it only when no session is running or starting ([`schedule`]): at once if the user has not
//! touched the app yet, else after two quiet minutes. The setting `update_mode: ask` waits for
//! the user's click instead. A server that needs a newer app (close 4426, HTTP 426) makes it
//! update at once, still never during a session. Each step reaches the windows as it happens
//! (`copilot://update`, the status bar) and is written to `updates.log` in the app's log
//! folder ([`journal`]) for support.

use serde::{Deserialize, Serialize};

/// apps/desktop/release.json. `infra/gcp/scripts/stage.py desktop-config` writes the URLs
/// from `terraform output desktop`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ReleaseConfig {
    /// Shown in the settings, e.g. "Stage 0 (Google Cloud)".
    pub stage: String,
    pub api_url: String,
    pub gateway_url: String,
    /// The update manifest (latest.json) in the public releases bucket.
    pub update_url: String,
}

#[cfg(feature = "release")]
pub fn config() -> Option<&'static ReleaseConfig> {
    use std::sync::OnceLock;
    static CONFIG: OnceLock<ReleaseConfig> = OnceLock::new();
    Some(CONFIG.get_or_init(|| {
        serde_json::from_str(include_str!(concat!(env!("OUT_DIR"), "/release.json")))
            .expect("release.json was checked by build.rs")
    }))
}

#[cfg(not(feature = "release"))]
pub fn config() -> Option<&'static ReleaseConfig> {
    None
}

/// What the update status bar and the settings show (`copilot://update`, `update_status`),
/// sent again at every step.
#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "state", rename_all = "snake_case")]
#[cfg_attr(not(feature = "release"), allow(dead_code))]
pub enum UpdateState {
    /// Not a release build, or no check has finished yet.
    None,
    /// Asking the releases bucket.
    Checking,
    /// The last check found nothing newer than `version` (the running one).
    UpToDate {
        version: String,
        checked_at: u64,
    },
    /// The last check could not reach the bucket; retried in half an hour.
    CheckFailed {
        message: String,
        checked_at: u64,
    },
    /// Downloading `version`; `percent` once the size is known.
    Downloading {
        version: String,
        percent: Option<u8>,
    },
    /// Downloaded and its signature verified. `auto`: it installs by itself when no session is
    /// running; otherwise ("ask me first") it waits for the button. `install_in_s`: seconds
    /// until it installs by itself, if nothing changes (None: not by itself, or a session runs).
    Ready {
        version: String,
        notes: Option<String>,
        auto: bool,
        install_in_s: Option<u64>,
    },
    Installing {
        version: String,
    },
    /// The app restarted into a new version: the "Updated to x.y.z" note.
    Updated {
        version: String,
    },
    /// The server needs a newer app than this one, and no update could be installed yet.
    Outdated {
        message: String,
    },
    /// A download or an install failed; retried in half an hour.
    Failed {
        message: String,
    },
}

impl UpdateState {
    /// Nothing under way or waiting: a new check may show "checking" over it.
    #[cfg_attr(not(feature = "release"), allow(dead_code))]
    pub fn settled(&self) -> bool {
        matches!(self, Self::None | Self::UpToDate { .. } | Self::CheckFailed { .. } | Self::Failed { .. })
    }
}

/// When to look for, and when to install, an update. Pure functions of the app's clock (the
/// time since it started) and what it was doing, so every rule is tested here.
#[cfg_attr(not(feature = "release"), allow(dead_code))]
pub mod schedule {
    use std::time::Duration;

    use serde::{Deserialize, Serialize};

    /// Between checks while the app runs.
    pub const CHECK_EVERY: Duration = Duration::from_secs(4 * 60 * 60);
    /// After a check, download or install that failed.
    pub const RETRY_AFTER: Duration = Duration::from_secs(30 * 60);
    /// "Quiet": no session, and no click, key or scroll in the app (nor a session's end) for
    /// this long. Short enough to come while the app is open, long enough not to restart
    /// under someone typing.
    pub const QUIET: Duration = Duration::from_secs(2 * 60);
    /// Between "installing" reaching the windows and the installer taking over.
    pub const SHOW_INSTALLING: Duration = Duration::from_millis(1500);
    /// How often the installer looks at the clock.
    pub const TICK: Duration = Duration::from_secs(10);

    #[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
    #[serde(rename_all = "snake_case")]
    pub enum UpdateMode {
        /// Install by itself when no session is running.
        #[default]
        Auto,
        /// Download, then wait for "Install and restart".
        Ask,
    }

    /// What the app was doing, on its own clock.
    #[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
    pub struct Activity {
        /// A session is running or starting.
        pub session: bool,
        /// The user's last click, key press or scroll in a window.
        pub last_input: Option<Duration>,
        pub last_session_end: Option<Duration>,
    }

    /// Whether a downloaded update may be installed now (`now` is the time since start).
    /// Never while a session runs or starts, whatever else holds. `urgent`: the server refused
    /// this version, so it installs as soon as no session runs, in either mode. Automatic: at
    /// once if the user has not touched the app (nor ended a session) since it started, else
    /// after [`QUIET`].
    pub fn may_install(now: Duration, a: &Activity, mode: UpdateMode, urgent: bool) -> bool {
        install_in(now, a, mode, urgent) == Some(Duration::ZERO)
    }

    /// How long until [`may_install`] holds if nothing else happens; None when it will not by
    /// itself (ask mode, or a session runs). The status bar's "installs in about a minute".
    pub fn install_in(now: Duration, a: &Activity, mode: UpdateMode, urgent: bool) -> Option<Duration> {
        if a.session {
            return None;
        }
        if urgent {
            return Some(Duration::ZERO);
        }
        if mode == UpdateMode::Ask {
            return None;
        }
        Some(match a.last_input.max(a.last_session_end) {
            None => Duration::ZERO,
            Some(t) => QUIET.saturating_sub(now.saturating_sub(t)),
        })
    }

    /// The wait before the next check: four hours after one that worked, half an hour after
    /// a failure (offline, the bucket unreachable, a download that broke off).
    pub fn next_check(ok: bool) -> Duration {
        if ok { CHECK_EVERY } else { RETRY_AFTER }
    }

    /// Starting a session and installing an update exclude each other: whichever claims first
    /// wins, and the other is refused until it is done. (`running`: an engine is live.)
    #[derive(Debug, Default)]
    pub struct Gate {
        inner: std::sync::Mutex<(bool, bool)>, // (starting, installing)
    }

    /// Marks a session as starting until dropped (start_session's whole run).
    pub struct Starting<'a>(&'a Gate);

    impl Drop for Starting<'_> {
        fn drop(&mut self) {
            self.0.inner.lock().unwrap().0 = false;
        }
    }

    impl Gate {
        pub fn starting(&self) -> bool {
            self.inner.lock().unwrap().0
        }

        pub fn begin_start(&self, running: bool) -> Result<Starting<'_>, &'static str> {
            let mut g = self.inner.lock().unwrap();
            if running || g.0 {
                return Err("a session is already running");
            }
            if g.1 {
                return Err("Installing an update; the app restarts in a moment.");
            }
            g.0 = true;
            Ok(Starting(self))
        }

        /// True if nothing runs or starts; from then on no session can start until
        /// [`Gate::install_failed`].
        pub fn claim_install(&self, running: bool) -> bool {
            let mut g = self.inner.lock().unwrap();
            if running || g.0 || g.1 {
                return false;
            }
            g.1 = true;
            true
        }

        pub fn install_failed(&self) {
            self.inner.lock().unwrap().1 = false;
        }
    }

    /// `a` is a newer version than `b` (semver; false if either does not parse).
    pub fn is_newer(a: &str, b: &str) -> bool {
        match (semver::Version::parse(a), semver::Version::parse(b)) {
            (Ok(a), Ok(b)) => a > b,
            _ => false,
        }
    }

    /// What the app finds at start, from the settings file: the version it last ran and the
    /// update it was installing when it exited.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum AfterStart {
        Nothing,
        /// Show "Updated to <current>".
        Updated {
            from: Option<String>,
        },
        /// The installer did not run through: still the old version. Retried at the next check.
        DidNotTake {
            version: String,
        },
    }

    pub fn after_start(last_version: Option<&str>, pending: Option<&str>, current: &str) -> AfterStart {
        match pending {
            Some(p) if p == current => AfterStart::Updated { from: last_version.map(str::to_string) },
            Some(p) => AfterStart::DidNotTake { version: p.to_string() },
            None => match last_version {
                Some(last) if is_newer(current, last) => AfterStart::Updated { from: Some(last.to_string()) },
                _ => AfterStart::Nothing,
            },
        }
    }
}

/// `updates.log`: one line per check, download and install, newest last, kept short.
pub mod journal {
    use std::io::Write;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    use serde::Serialize;

    /// Over this size the file is cut to its last [`KEEP`] lines.
    const MAX_BYTES: u64 = 64 * 1024;
    const KEEP: usize = 200;

    #[derive(Debug, Clone, Serialize, PartialEq, Eq)]
    pub struct Entry {
        /// Unix time, milliseconds.
        pub at_ms: u64,
        pub message: String,
    }

    pub fn now_ms() -> u64 {
        SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64)
    }

    #[derive(Debug, Clone)]
    pub struct Journal {
        path: PathBuf,
    }

    impl Journal {
        pub fn new(path: PathBuf) -> Self {
            Self { path }
        }

        /// Also to the console log. A file that cannot be written loses the line, nothing more.
        pub fn write(&self, message: &str) {
            tracing::info!("update: {message}");
            self.write_at(now_ms(), message);
        }

        pub fn write_at(&self, at_ms: u64, message: &str) {
            if let Some(dir) = self.path.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            let line = format!("{at_ms}\t{}\n", message.replace(['\n', '\r'], " "));
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&self.path) {
                let _ = f.write_all(line.as_bytes());
            }
            if std::fs::metadata(&self.path).is_ok_and(|m| m.len() > MAX_BYTES) {
                let kept = self.lines(KEEP);
                let text: String = kept.iter().map(|e| format!("{}\t{}\n", e.at_ms, e.message)).collect();
                let _ = std::fs::write(&self.path, text);
            }
        }

        /// The last `n` entries, oldest first.
        pub fn lines(&self, n: usize) -> Vec<Entry> {
            let text = std::fs::read_to_string(&self.path).unwrap_or_default();
            let all: Vec<Entry> = text
                .lines()
                .filter_map(|l| {
                    let (at, message) = l.split_once('\t')?;
                    Some(Entry { at_ms: at.parse().ok()?, message: message.to_string() })
                })
                .collect();
            all[all.len().saturating_sub(n)..].to_vec()
        }
    }
}

/// The updater itself: the check loop and the installer, around the Tauri updater plugin
/// (which verifies the download's signature against the public key built into the app).
#[cfg(feature = "release")]
pub mod updater {
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, Ordering};

    use tauri::{AppHandle, Emitter, Manager};
    use tauri_plugin_updater::{Update, UpdaterExt};

    use super::UpdateState;
    use super::journal::{Journal, now_ms};
    use super::schedule::{self, UpdateMode};
    use crate::AppState;

    pub const EVENT: &str = "copilot://update";

    pub struct Updates {
        /// Downloaded and verified, waiting for its moment.
        ready: Mutex<Option<(Update, Vec<u8>)>>,
        state: Mutex<UpdateState>,
        /// The server refused this version: install as soon as no session runs.
        urgent: AtomicBool,
        /// Check now (urgent, "Check now", a retry after a failed install).
        wake: tokio::sync::Notify,
        checked_at: Mutex<Option<u64>>,
        /// This run started right after an update to this version: the "Updated to" note,
        /// kept until something newer is under way.
        updated_to: Option<String>,
        pub journal: Journal,
    }

    impl Updates {
        pub fn new(journal: Journal, updated_to: Option<String>) -> Self {
            let state = match &updated_to {
                Some(version) => UpdateState::Updated { version: version.clone() },
                None => UpdateState::None,
            };
            Self {
                ready: Mutex::new(None),
                state: Mutex::new(state),
                urgent: AtomicBool::new(false),
                wake: tokio::sync::Notify::new(),
                checked_at: Mutex::new(None),
                updated_to,
                journal,
            }
        }

        pub fn state(&self) -> UpdateState {
            self.state.lock().unwrap().clone()
        }

        pub fn checked_at(&self) -> Option<u64> {
            *self.checked_at.lock().unwrap()
        }

        /// Sends the state to the windows only when it changed.
        pub fn set(&self, app: &AppHandle, state: UpdateState) {
            {
                let mut cur = self.state.lock().unwrap();
                if *cur == state {
                    return;
                }
                *cur = state.clone();
            }
            let _ = app.emit(EVENT, state);
        }

        pub fn check_now(&self) {
            self.wake.notify_one();
        }

        /// After a check that found nothing: the "Updated to" note if this run has one, else
        /// "up to date".
        fn nothing_new(&self, running: String, checked_at: u64) -> UpdateState {
            match &self.updated_to {
                Some(version) => UpdateState::Updated { version: version.clone() },
                None => UpdateState::UpToDate { version: running, checked_at },
            }
        }
    }

    fn mode(app: &AppHandle) -> UpdateMode {
        app.state::<AppState>().settings.lock().unwrap().update_mode
    }

    /// At start, after the setup: the check loop and the installer.
    pub fn start(app: &AppHandle) {
        let a = app.clone();
        tauri::async_runtime::spawn(async move {
            loop {
                let ok = check_once(&a).await;
                let wait = schedule::next_check(ok);
                let updates = a.state::<Updates>();
                tokio::select! {
                    _ = tokio::time::sleep(wait) => {}
                    _ = updates.wake.notified() => {}
                }
            }
        });
        let a = app.clone();
        tauri::async_runtime::spawn(async move {
            loop {
                tokio::time::sleep(schedule::TICK).await;
                refresh_ready(&a);
                if let Err(e) = install(&a, false) {
                    tracing::debug!("update not installed: {e}");
                }
            }
        });
    }

    /// The downloaded update's state as of now: the mode (the setting may have changed) and
    /// the time until it installs by itself (the status bar counts down with it).
    fn ready_state(app: &AppHandle, update: &Update) -> UpdateState {
        let state = app.state::<AppState>();
        let urgent = app.state::<Updates>().urgent.load(Ordering::SeqCst);
        let mode = mode(app);
        let install_in = schedule::install_in(state.clock(), &state.activity(), mode, urgent);
        UpdateState::Ready {
            version: update.version.clone(),
            notes: update.body.clone(),
            auto: mode == UpdateMode::Auto || urgent,
            install_in_s: install_in.map(|d| d.as_secs()),
        }
    }

    /// Re-sends "ready" if the mode, a session or the countdown changed it.
    pub fn refresh_ready(app: &AppHandle) {
        let updates = app.state::<Updates>();
        let next = match updates.ready.lock().unwrap().as_ref() {
            Some((update, _)) => ready_state(app, update),
            None => return,
        };
        if matches!(updates.state(), UpdateState::Ready { .. }) {
            updates.set(app, next);
        }
    }

    /// The server refused this version (4426, HTTP 426): check and install at once.
    pub fn urgent(app: &AppHandle, why: &str) {
        let updates = app.state::<Updates>();
        if !updates.urgent.swap(true, Ordering::SeqCst) {
            updates.journal.write(&format!("the server needs a newer version ({why}); updating now"));
        }
        if updates.ready.lock().unwrap().is_some() {
            refresh_ready(app);
            let _ = install(app, false);
        } else {
            updates.check_now();
        }
    }

    /// Ask the releases bucket; download and verify anything newer. False on a failure, for
    /// an earlier retry.
    async fn check_once(app: &AppHandle) -> bool {
        let updates = app.state::<Updates>();
        let running = app.package_info().version.to_string();
        // Over a settled state only: the "Updated to" note or a waiting update stays shown.
        if updates.state().settled() {
            updates.set(app, UpdateState::Checking);
        }
        let found = match app.updater() {
            Ok(u) => u.check().await,
            Err(e) => Err(e),
        };
        let checked_at = now_ms();
        *updates.checked_at.lock().unwrap() = Some(checked_at);
        let urgent = updates.urgent.load(Ordering::SeqCst);
        let was_checking = updates.state() == UpdateState::Checking;
        let update = match found {
            Ok(Some(update)) => update,
            Ok(None) => {
                updates.journal.write(&format!("checked: {running} is the newest"));
                if urgent {
                    updates.set(app, UpdateState::Outdated {
                        message: format!(
                            "This version ({running}) is older than the server supports, and no newer one is published \
                             yet. Download the installer again, or wait for the next release."
                        ),
                    });
                } else if was_checking {
                    updates.set(app, updates.nothing_new(running, checked_at));
                }
                return true;
            }
            Err(e) => {
                updates.journal.write(&format!("check failed: {e}; retrying in 30 minutes"));
                if was_checking {
                    updates.set(
                        app,
                        UpdateState::CheckFailed {
                            message: format!(
                                "Could not check for updates ({e}). Trying again in 30 minutes."
                            ),
                            checked_at,
                        },
                    );
                }
                return false;
            }
        };
        let have = updates.ready.lock().unwrap().as_ref().map(|(u, _)| u.version.clone());
        if have.as_deref() == Some(update.version.as_str()) {
            return true;
        }
        let version = update.version.clone();
        updates.journal.write(&format!("found {version} (running {running}); downloading"));
        updates.set(app, UpdateState::Downloading { version: version.clone(), percent: None });
        // download() verifies the signature (and the signed version) before it returns.
        let progress = {
            let app = app.clone();
            let version = version.clone();
            let mut got: u64 = 0;
            move |chunk: usize, total: Option<u64>| {
                got += chunk as u64;
                let percent = total.filter(|t| *t > 0).map(|t| (got.min(t) * 100 / t) as u8);
                // set() sends only when the percent moves.
                app.state::<Updates>()
                    .set(&app, UpdateState::Downloading { version: version.clone(), percent });
            }
        };
        match update.download(progress, || {}).await {
            Ok(bytes) => {
                updates.journal.write(&format!("{version} downloaded and verified"));
                let state = ready_state(app, &update);
                *updates.ready.lock().unwrap() = Some((update, bytes));
                updates.set(app, state);
                // At once if this is a good moment (or the server insists), not a tick later.
                let _ = install(app, false);
                true
            }
            Err(e) => {
                updates.journal.write(&format!("download of {version} failed: {e}; retrying in 30 minutes"));
                let older = updates.ready.lock().unwrap().as_ref().map(|(u, _)| ready_state(app, u));
                // An older download still waits: show that one instead.
                updates.set(
                    app,
                    older.unwrap_or(UpdateState::Failed {
                        message: format!(
                            "The download of version {version} failed ({e}). Trying again in 30 minutes."
                        ),
                    }),
                );
                false
            }
        }
    }

    /// Install the downloaded update if this is a good moment (`by_user`: the button, which
    /// skips the timing but not the session check). "Installing" reaches the windows first;
    /// then, on Windows, the installer starts and the app exits, and the installer restarts
    /// it. A failure leaves this version running and retries later.
    pub fn install(app: &AppHandle, by_user: bool) -> Result<(), String> {
        let state = app.state::<AppState>();
        let updates = app.state::<Updates>();
        if updates.ready.lock().unwrap().is_none() {
            return Err("no update downloaded".into());
        }
        let urgent = updates.urgent.load(Ordering::SeqCst);
        let mode = mode(app);
        if !by_user && !schedule::may_install(state.clock(), &state.activity(), mode, urgent) {
            return Err("not now".into());
        }
        // Claim the moment: no session may start from here on (start_session checks).
        if !state.claim_for_install() {
            return Err("End the session first; the app restarts to update.".into());
        }
        let Some((update, bytes)) = updates.ready.lock().unwrap().take() else {
            state.gate.install_failed();
            return Err("no update downloaded".into());
        };
        let version = update.version.clone();
        updates.journal.write(&format!(
            "installing {version} ({})",
            if by_user {
                "by request"
            } else if urgent {
                "required by the server"
            } else {
                "no session running"
            }
        ));
        updates.set(app, UpdateState::Installing { version: version.clone() });
        state.update_settings(|s| s.pending_update = Some(version.clone()));
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            // Time for the status bar to say so before the installer takes over.
            tokio::time::sleep(schedule::SHOW_INSTALLING).await;
            let result = update.install(&bytes);
            let state = app.state::<AppState>();
            let updates = app.state::<Updates>();
            match result {
                Ok(()) => app.restart(),
                Err(e) => {
                    let running = app.package_info().version.to_string();
                    updates.journal.write(&format!(
                        "the update to {version} failed: {e}; still on {running}; retrying in 30 minutes"
                    ));
                    state.update_settings(|s| s.pending_update = None);
                    state.gate.install_failed();
                    updates.set(&app, UpdateState::Failed {
                        message: format!(
                            "The update to version {version} failed ({e}); still on {running}. Trying again in 30 minutes."
                        ),
                    });
                    tokio::time::sleep(schedule::RETRY_AFTER).await;
                    updates.check_now();
                }
            }
        });
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::journal::Journal;
    use super::schedule::*;

    const S: Duration = Duration::from_secs(1);

    fn idle() -> Activity {
        Activity::default()
    }

    #[test]
    fn never_during_a_session_even_when_urgent() {
        let busy = Activity { session: true, ..idle() };
        for now in [S, 30 * S, 3 * 3600 * S] {
            for mode in [UpdateMode::Auto, UpdateMode::Ask] {
                for urgent in [false, true] {
                    assert!(!may_install(now, &busy, mode, urgent), "{now:?} {mode:?} {urgent}");
                }
            }
        }
        // Long after the last input and the last session, but a session is starting.
        let starting = Activity { session: true, last_input: Some(S), last_session_end: Some(S) };
        assert!(!may_install(3600 * S, &starting, UpdateMode::Auto, false));
    }

    #[test]
    fn at_once_before_the_user_does_anything() {
        assert!(may_install(10 * S, &idle(), UpdateMode::Auto, false));
        // Long after start too: an app left open and untouched updates.
        assert!(may_install(3 * 3600 * S, &idle(), UpdateMode::Auto, false));
        assert_eq!(install_in(10 * S, &idle(), UpdateMode::Auto, false), Some(Duration::ZERO));
    }

    #[test]
    fn after_two_quiet_minutes() {
        let used = Activity { last_input: Some(100 * S), ..idle() };
        assert!(!may_install(100 * S + QUIET - S, &used, UpdateMode::Auto, false));
        assert_eq!(install_in(130 * S, &used, UpdateMode::Auto, false), Some(QUIET - 30 * S));
        assert!(may_install(100 * S + QUIET, &used, UpdateMode::Auto, false));
        // A click starts the wait again.
        let again = Activity { last_input: Some(100 * S + QUIET - S), ..used };
        assert!(!may_install(100 * S + QUIET, &again, UpdateMode::Auto, false));
    }

    #[test]
    fn after_a_session_ends_the_same_quiet_time() {
        let end = 600 * S;
        let ended = Activity { last_input: Some(end - 30 * S), last_session_end: Some(end), ..idle() };
        assert!(!may_install(end + 10 * S, &ended, UpdateMode::Auto, false));
        assert!(may_install(end + QUIET, &ended, UpdateMode::Auto, false));
        // The user is reading the post-call screen: wait until they stop.
        let reading = Activity { last_input: Some(end + 20 * S), ..ended };
        assert!(!may_install(end + QUIET, &reading, UpdateMode::Auto, false));
        assert!(may_install(end + 20 * S + QUIET, &reading, UpdateMode::Auto, false));
    }

    #[test]
    fn ask_mode_waits_for_the_button_unless_the_server_insists() {
        assert!(!may_install(10 * S, &idle(), UpdateMode::Ask, false));
        assert!(!may_install(10 * QUIET, &idle(), UpdateMode::Ask, false));
        assert_eq!(install_in(10 * S, &idle(), UpdateMode::Ask, false), None);
        let used = Activity { last_input: Some(9 * S), ..idle() };
        assert!(may_install(10 * S, &used, UpdateMode::Ask, true));
        assert!(may_install(10 * S, &used, UpdateMode::Auto, true));
        let busy = Activity { session: true, ..idle() };
        assert_eq!(install_in(10 * S, &busy, UpdateMode::Auto, true), None);
    }

    #[test]
    fn a_session_start_and_an_install_never_overlap() {
        let gate = Gate::default();
        // A session is starting: no install, and no second start.
        let starting = gate.begin_start(false).unwrap();
        assert!(!gate.claim_install(false));
        assert!(gate.begin_start(false).is_err());
        drop(starting);
        // Running (the engine is live): still no install.
        assert!(!gate.claim_install(true));
        // Nothing runs: the install claims it, and a start is refused until it fails.
        assert!(gate.claim_install(false));
        assert!(!gate.claim_install(false));
        assert_eq!(
            gate.begin_start(false).err(),
            Some("Installing an update; the app restarts in a moment.")
        );
        gate.install_failed();
        assert!(gate.begin_start(false).is_ok());
    }

    #[test]
    fn checks_every_four_hours_and_retry_sooner() {
        assert_eq!(next_check(true), Duration::from_secs(4 * 3600));
        assert_eq!(next_check(false), Duration::from_secs(30 * 60));
    }

    #[test]
    fn after_a_restart() {
        assert_eq!(
            after_start(Some("0.1.1"), Some("0.1.2"), "0.1.2"),
            AfterStart::Updated { from: Some("0.1.1".into()) }
        );
        assert_eq!(
            after_start(Some("0.1.1"), Some("0.1.2"), "0.1.1"),
            AfterStart::DidNotTake { version: "0.1.2".into() }
        );
        // Installed by hand, or by 0.1.0 (which kept no pending update).
        assert_eq!(
            after_start(Some("0.1.1"), None, "0.2.0"),
            AfterStart::Updated { from: Some("0.1.1".into()) }
        );
        assert_eq!(after_start(None, None, "0.1.1"), AfterStart::Nothing);
        assert_eq!(after_start(Some("0.1.1"), None, "0.1.1"), AfterStart::Nothing);
        assert!(is_newer("0.1.10", "0.1.9"));
        assert!(!is_newer("0.1.0", "0.1.0"));
        assert!(!is_newer("x", "0.1.0"));
    }

    #[test]
    fn the_log_keeps_the_newest_lines() {
        let path = std::env::temp_dir().join(format!(
            "copilot-updates-{}.log",
            super::journal::now_ms() + u64::from(std::process::id())
        ));
        let j = Journal::new(path.clone());
        j.write_at(1, "checked: 0.1.1 is the newest");
        j.write_at(2, "found 0.1.2\nwith a newline");
        let lines = j.lines(10);
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[1].at_ms, 2);
        assert_eq!(lines[1].message, "found 0.1.2 with a newline");
        for i in 0..2000 {
            j.write_at(10 + i, &format!("line {i} {}", "x".repeat(40)));
        }
        assert!(std::fs::metadata(&path).unwrap().len() <= 64 * 1024 + 100);
        assert_eq!(j.lines(1)[0].message, format!("line 1999 {}", "x".repeat(40)));
        let _ = std::fs::remove_file(path);
    }
}
