//! Opt-in, local Android call-state metadata. Never captures audio or starts a call.
//!
//! AOSP's redacted dump can still contain action labels and object descriptions. Raw
//! output is bounded, held only while parsing, and never returned in diagnostics.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::time::{Duration, Instant};

#[path = "hushed_telecom.rs"]
mod telecom;

const PACKAGE: &str = "com.hushed.release";
const MAX_DEVICES: usize = 4;
const MAX_OUTPUT: usize = 256 * 1024;
const COMMAND_TIMEOUT: Duration = Duration::from_millis(1500);
const PACKAGE_CHECK_TTL: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct Config {
    pub enabled: bool,
    pub adb_path: String,
    pub serials: Vec<String>,
}

impl Config {
    pub fn validate(&self) -> Result<(), String> {
        if self.serials.len() > MAX_DEVICES {
            return Err("Choose at most four local Android emulators.".into());
        }
        let mut endpoints = BTreeSet::new();
        for serial in &self.serials {
            let endpoint = local_endpoint(serial).ok_or("Use a local emulator serial or 127.0.0.1:port.")?;
            if !endpoints.insert(endpoint) {
                return Err("Choose each local emulator only once.".into());
            }
        }
        if self.enabled {
            validate_adb(&self.adb_path)?;
            if self.serials.is_empty() {
                return Err("Choose a local emulator before enabling Hushed detection.".into());
            }
        }
        Ok(())
    }
}

fn validate_adb(value: &str) -> Result<(), String> {
    let path = Path::new(value);
    let expected = if cfg!(windows) { "adb.exe" } else { "adb" };
    if !path.is_absolute()
        || !path.is_file()
        || !path.file_name().and_then(|v| v.to_str()).is_some_and(|v| v.eq_ignore_ascii_case(expected))
    {
        return Err("Choose the existing adb executable using its full path.".into());
    }
    Ok(())
}

/// Canonical endpoint also rejects configuring emulator-5554 and 127.0.0.1:5555 twice.
fn local_endpoint(serial: &str) -> Option<u16> {
    let (digits, console) = if let Some(value) = serial.strip_prefix("127.0.0.1:") {
        (value, false)
    } else {
        (serial.strip_prefix("emulator-")?, true)
    };
    if digits.is_empty() || digits.starts_with('0') || !digits.bytes().all(|v| v.is_ascii_digit()) {
        return None;
    }
    let port = digits.parse::<u16>().ok()?;
    if console { port.checked_add(1) } else { Some(port) }
}

#[derive(Clone, Debug, Default, Serialize)]
pub(super) struct Status {
    pub state: String,
    pub message: String,
    pub connected: usize,
    pub monitored: usize,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) struct Call {
    pub key: String,
    pub incoming: bool,
}

#[derive(Default)]
pub(super) struct Sample {
    pub calls: Vec<Call>,
    pub retired: Vec<String>,
    pub forgotten: Vec<String>,
    /// A connecting/disconnecting call is present without enough evidence to offer assistance.
    pub block_end_review: bool,
    pub status: Status,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Observation {
    Idle,
    Incoming,
    Active,
    Unsupported,
    Unavailable,
    NotInstalled,
}

#[derive(Default)]
struct Tracked {
    key: String,
    missing: u8,
    incoming: bool,
    unconfirmed: bool,
}

#[derive(Debug, PartialEq, Eq)]
enum Source {
    Notification,
    Telecom(String),
}

struct Device {
    source: Source,
    calls: BTreeMap<String, Tracked>,
}

enum DeviceObservation {
    Notification(Observation),
    Telecom { epoch: String, calls: Vec<telecom::Call> },
    Unknown(Observation),
}

#[derive(Default)]
pub(super) struct Sampler {
    occurrences: BTreeMap<String, Device>,
    sequence: u64,
    adb_path: String,
    installed: BTreeMap<String, (Instant, bool)>,
}

impl Sampler {
    pub fn sample(&mut self, config: &Config) -> Sample {
        if !config.enabled {
            return self.clear("disabled", "Hushed detection is off.");
        }
        if let Err(message) = config.validate() {
            return Sample {
                status: Status { state: "configuration_error".into(), message, ..Status::default() },
                ..Sample::default()
            };
        }
        if !cfg!(windows) {
            return self.clear("unavailable", "Local LDPlayer detection is available on Windows.");
        }
        let mut forgotten = Vec::new();
        if self.adb_path != config.adb_path {
            forgotten = self.take_keys();
            self.installed.clear();
            self.adb_path.clone_from(&config.adb_path);
        }
        self.installed.retain(|serial, _| config.serials.contains(serial));
        // Fixed, bounded commands; instances run concurrently. No raw dump escapes the probe.
        let observations = std::thread::scope(|scope| {
            let handles: Vec<_> = config
                .serials
                .iter()
                .map(|serial| {
                    let cached = self
                        .installed
                        .get(serial)
                        .filter(|(time, _)| time.elapsed() < PACKAGE_CHECK_TTL)
                        .map(|(_, installed)| *installed);
                    let prefer_telecom = self
                        .occurrences
                        .get(serial)
                        .is_some_and(|device| matches!(device.source, Source::Telecom(_)));
                    scope.spawn(move || {
                        let installed = match cached {
                            Some(value) => Ok(Some(value)),
                            None => run_adb(
                                &config.adb_path,
                                &["-s", serial.as_str(), "shell", "pm", "list", "packages", PACKAGE],
                            )
                            .map(|bytes| parse_package_list(&bytes)),
                        };
                        let installed = match installed {
                            Ok(Some(value)) => value,
                            _ => {
                                return (
                                    serial.clone(),
                                    DeviceObservation::Unknown(Observation::Unavailable),
                                    None,
                                );
                            }
                        };
                        if !installed {
                            return (
                                serial.clone(),
                                DeviceObservation::Unknown(Observation::NotInstalled),
                                Some(false),
                            );
                        }
                        (serial.clone(), probe(&config.adb_path, serial, prefer_telecom), Some(true))
                    })
                })
                .collect();
            handles
                .into_iter()
                .zip(&config.serials)
                .map(|(handle, serial)| {
                    handle.join().unwrap_or_else(|_| {
                        (serial.clone(), DeviceObservation::Unknown(Observation::Unavailable), None)
                    })
                })
                .collect::<Vec<_>>()
        });
        for (serial, _, installed) in &observations {
            if let Some(installed) = installed {
                self.installed
                    .entry(serial.clone())
                    .and_modify(|(time, value)| {
                        if time.elapsed() >= PACKAGE_CHECK_TTL || *value != *installed {
                            *time = Instant::now();
                            *value = *installed;
                        }
                    })
                    .or_insert((Instant::now(), *installed));
            } else {
                self.installed.remove(serial);
            }
        }
        let states = observations.into_iter().map(|(serial, state, _)| (serial, state)).collect::<Vec<_>>();
        let mut result = self.apply_devices(&config.serials, &states);
        result.forgotten.extend(forgotten);
        result
    }

    fn take_keys(&mut self) -> Vec<String> {
        std::mem::take(&mut self.occurrences)
            .into_values()
            .flat_map(|device| device.calls.into_values().map(|call| call.key))
            .collect()
    }

    fn clear(&mut self, state: &str, message: &str) -> Sample {
        let forgotten = self.take_keys();
        self.installed.clear();
        Sample {
            forgotten,
            status: Status { state: state.into(), message: message.into(), ..Status::default() },
            ..Sample::default()
        }
    }

    #[cfg(test)]
    fn apply(&mut self, selected: &[String], observations: &[(String, Observation)]) -> Sample {
        let observations = observations
            .iter()
            .map(|(serial, state)| (serial.clone(), DeviceObservation::Notification(*state)))
            .collect::<Vec<_>>();
        self.apply_devices(selected, &observations)
    }

    fn apply_devices(&mut self, selected: &[String], observations: &[(String, DeviceObservation)]) -> Sample {
        let mut result = Sample::default();
        self.occurrences.retain(|serial, device| {
            if selected.contains(serial) {
                true
            } else {
                result.forgotten.extend(device.calls.values().map(|call| call.key.clone()));
                false
            }
        });
        let mut states = Vec::new();
        for (serial, observation) in observations {
            let (source, current, state) = match observation {
                DeviceObservation::Unknown(state) => {
                    self.break_absence(serial);
                    states.push(*state);
                    continue;
                }
                DeviceObservation::Notification(state) => {
                    if matches!(
                        state,
                        Observation::Unsupported | Observation::Unavailable | Observation::NotInstalled
                    ) {
                        self.break_absence(serial);
                        states.push(*state);
                        continue;
                    }
                    let current = match state {
                        Observation::Incoming => vec![("notification".into(), telecom::Phase::Incoming)],
                        Observation::Active => vec![("notification".into(), telecom::Phase::Active)],
                        _ => Vec::new(),
                    };
                    (Source::Notification, current, *state)
                }
                DeviceObservation::Telecom { epoch, calls } => {
                    let current = calls
                        .iter()
                        .filter(|call| call.phase != telecom::Phase::Ended)
                        .map(|call| (call.id.clone(), call.phase))
                        .collect::<Vec<_>>();
                    let state = if calls.iter().any(|call| call.phase == telecom::Phase::Incoming) {
                        Observation::Incoming
                    } else if calls.iter().any(|call| call.phase == telecom::Phase::Active) {
                        Observation::Active
                    } else {
                        Observation::Idle
                    };
                    (Source::Telecom(epoch.clone()), current, state)
                }
            };
            states.push(state);
            let mut device = self
                .occurrences
                .remove(serial)
                .unwrap_or_else(|| Device { source: Source::Notification, calls: BTreeMap::new() });
            if device.source != source {
                // Migrate a single notification to one unambiguous Telecom call without
                // re-prompting a dismissed call. Process/boot changes never reuse old IDs.
                let can_transfer = matches!(device.source, Source::Notification)
                    && matches!(source, Source::Telecom(_))
                    && device.calls.len() == 1
                    && current.len() == 1
                    && device
                        .calls
                        .values()
                        .next()
                        .is_some_and(|old| old.incoming || current[0].1 != telecom::Phase::Incoming);
                if can_transfer {
                    let old = std::mem::take(&mut device.calls).into_values().next().expect("one call");
                    device.calls.insert(current[0].0.clone(), old);
                } else if current.is_empty() && matches!(device.source, Source::Notification) {
                    // Keep the legacy identity until two healthy samples confirm absence.
                } else {
                    result
                        .forgotten
                        .extend(std::mem::take(&mut device.calls).into_values().map(|call| call.key));
                }
                if !current.is_empty() || !matches!(device.source, Source::Notification) {
                    device.source = source;
                }
            }
            for (id, phase) in &current {
                if *phase == telecom::Phase::Transitional {
                    let tracked = device.calls.entry(id.clone()).or_insert_with(|| {
                        self.sequence = self.sequence.wrapping_add(1);
                        Tracked {
                            key: format!("android-{}:Hushed", self.sequence),
                            unconfirmed: true,
                            ..Default::default()
                        }
                    });
                    tracked.missing = 0;
                    continue;
                }
                let incoming = *phase == telecom::Phase::Incoming;
                // Notifications have no safe stable call ID. Telecom IDs already
                // distinguish consecutive calls, including calls with no idle poll.
                if matches!(device.source, Source::Notification)
                    && incoming
                    && device.calls.get(id).is_some_and(|call| !call.incoming)
                    && let Some(previous) = device.calls.remove(id)
                {
                    result.retired.push(previous.key);
                }
                let tracked = device.calls.entry(id.clone()).or_insert_with(|| {
                    self.sequence = self.sequence.wrapping_add(1);
                    Tracked {
                        key: format!("android-{}:Hushed", self.sequence),
                        missing: 0,
                        incoming,
                        unconfirmed: false,
                    }
                });
                tracked.missing = 0;
                tracked.incoming = incoming;
                tracked.unconfirmed = false;
                result.calls.push(Call { key: tracked.key.clone(), incoming });
            }
            device.calls.retain(|id, tracked| {
                if current.iter().any(|(current_id, _)| current_id == id) {
                    return true;
                }
                tracked.missing += 1;
                if tracked.missing >= 2 {
                    result.block_end_review |= tracked.unconfirmed;
                    result.retired.push(tracked.key.clone());
                    false
                } else {
                    true
                }
            });
            // Once no lifecycle needs protecting, an unregistered provider can fall
            // back to supported notification metadata again.
            if !device.calls.is_empty() {
                self.occurrences.insert(serial.clone(), device);
            }
        }
        result.block_end_review |=
            self.occurrences.values().any(|device| device.calls.values().any(|call| call.unconfirmed));
        let unsupported = states.iter().filter(|state| **state == Observation::Unsupported).count();
        let unavailable = states.iter().filter(|state| **state == Observation::Unavailable).count();
        let missing_package = states.iter().filter(|state| **state == Observation::NotInstalled).count();
        let connected = states.len().saturating_sub(unavailable);
        let (state, message) = if unavailable + unsupported + missing_package > 0
            && connected > unsupported + missing_package
        {
            ("partial", "Some emulators are unavailable, lack Hushed, or expose unsupported call metadata.")
        } else if unsupported > 0 {
            (
                "unsupported",
                "Hushed call metadata is not supported on this emulator. Start assistance manually.",
            )
        } else if missing_package > 0 {
            ("not_installed", "Hushed is not installed on the selected local emulator.")
        } else if unavailable > 0 {
            ("unavailable", "Local emulator connection unavailable. Check ADB and the selected emulator.")
        } else if result.calls.iter().any(|call| call.incoming) {
            ("incoming", "Hushed incoming call detected.")
        } else if !result.calls.is_empty() {
            ("active", "Hushed ongoing call detected.")
        } else {
            (
                "idle",
                "Monitoring local Hushed call state. Real Hushed call behavior still needs verification.",
            )
        };
        result.status =
            Status { state: state.into(), message: message.into(), connected, monitored: selected.len() };
        result
    }

    fn break_absence(&mut self, serial: &str) {
        if let Some(device) = self.occurrences.get_mut(serial) {
            for call in device.calls.values_mut() {
                call.missing = 0;
            }
        }
    }
}

fn telecom_pid(adb: &str, serial: &str) -> Option<String> {
    // Ask Binder which process owns the service; emulator builds can host it in
    // system_server rather than a process named com.android.server.telecom.
    let bytes = run_adb(adb, &["-s", serial, "shell", "dumpsys", "--pid", "telecom"]).ok()?;
    parse_pid(&bytes)
}

fn boot_id(adb: &str, serial: &str) -> Option<String> {
    run_adb(adb, &["-s", serial, "shell", "cat", "/proc/sys/kernel/random/boot_id"])
        .ok()
        .and_then(|bytes| parse_boot_id(&bytes))
}

fn parse_pid(bytes: &[u8]) -> Option<String> {
    let value = std::str::from_utf8(bytes).ok()?.trim();
    if value.is_empty()
        || value.len() > 10
        || !value.bytes().all(|byte| byte.is_ascii_digit())
        || value.parse::<u32>().ok()? == 0
    {
        return None;
    }
    Some(value.into())
}

fn parse_boot_id(bytes: &[u8]) -> Option<String> {
    let value = std::str::from_utf8(bytes).ok()?.trim();
    if value.len() != 36
        || !value.bytes().enumerate().all(|(i, byte)| {
            if [8, 13, 18, 23].contains(&i) { byte == b'-' } else { byte.is_ascii_hexdigit() }
        })
    {
        return None;
    }
    Some(value.to_ascii_lowercase())
}

fn probe(adb: &str, serial: &str, prefer_telecom: bool) -> DeviceObservation {
    // Check process identity around the snapshot. Boot ID protects emulator reboots.
    let boot_before = boot_id(adb, serial);
    let pid_before = telecom_pid(adb, serial);
    let snapshot = match run_adb(adb, &["-s", serial, "shell", "dumpsys", "telecom"]) {
        Ok(bytes) => std::str::from_utf8(&bytes).map(telecom::parse).unwrap_or(telecom::Snapshot::Unknown),
        Err(_) if prefer_telecom => return DeviceObservation::Unknown(Observation::Unavailable),
        Err(_) => telecom::Snapshot::Unknown,
    };
    let calls = match snapshot {
        telecom::Snapshot::Registered(calls) => Some(calls),
        telecom::Snapshot::Unregistered if prefer_telecom => Some(Vec::new()),
        telecom::Snapshot::Unknown if prefer_telecom => {
            return DeviceObservation::Unknown(Observation::Unsupported);
        }
        _ => None,
    };
    if let Some(calls) = calls {
        let pid_after = telecom_pid(adb, serial);
        let boot_after = boot_id(adb, serial);
        return match (boot_before, boot_after, pid_before, pid_after) {
            (Some(first_boot), Some(last_boot), Some(before), Some(after))
                if before == after && first_boot == last_boot =>
            {
                DeviceObservation::Telecom { epoch: format!("{last_boot}:{after}"), calls }
            }
            _ => DeviceObservation::Unknown(Observation::Unavailable),
        };
    }
    let state = match run_adb(
        adb,
        &[
            "-s",
            serial,
            "shell",
            "dumpsys",
            "notification",
            "--dump-priority",
            "CRITICAL",
            "--package",
            PACKAGE,
        ],
    ) {
        Ok(bytes) => std::str::from_utf8(&bytes).map(parse_notifications).unwrap_or(Observation::Unsupported),
        Err(_) => Observation::Unavailable,
    };
    DeviceObservation::Notification(state)
}

fn parse_package_list(bytes: &[u8]) -> Option<bool> {
    let text = std::str::from_utf8(bytes).ok()?.trim();
    let mut installed = false;
    // `pm path` exits 1 for an absent package. The filtered list succeeds with
    // empty output, but its filter is a substring, so require the exact name.
    for line in text.lines() {
        let package = line.trim().strip_prefix("package:")?;
        if !package.contains(PACKAGE)
            || !package.split('.').all(|part| {
                !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            })
        {
            return None;
        }
        installed |= package == PACKAGE;
    }
    Some(installed)
}

/// Lists already connected local endpoints only; does not connect or change emulator settings.
pub(super) fn devices(adb_path: &str) -> Result<Vec<String>, String> {
    validate_adb(adb_path)?;
    let output = run_adb(adb_path, &["devices"]).map_err(|_| "Could not list local Android emulators.")?;
    parse_devices(std::str::from_utf8(&output).map_err(|_| "ADB returned an unsupported device list.")?)
}

/// Only an explicit setup action calls this. Background sampling never connects devices.
pub(super) fn connect(adb_path: &str, serial: &str) -> Result<Vec<String>, String> {
    validate_adb(adb_path)?;
    if !serial.starts_with("127.0.0.1:") || local_endpoint(serial).is_none() {
        return Err("Connect only to a local emulator using 127.0.0.1:port.".into());
    }
    run_adb(adb_path, &["connect", serial]).map_err(|_| "Could not connect to the local emulator.")?;
    let listed = devices(adb_path)?;
    let expected = local_endpoint(serial);
    if !listed.iter().any(|value| local_endpoint(value) == expected) {
        return Err("The local emulator is not connected. Check its local ADB setting and port.".into());
    }
    Ok(listed)
}

fn parse_devices(output: &str) -> Result<Vec<String>, String> {
    let mut header = false;
    let mut endpoints = BTreeMap::new();
    for line in output.lines().map(str::trim).filter(|v| !v.is_empty()) {
        if line == "List of devices attached" {
            header = true;
            continue;
        }
        if !header {
            return Err("ADB returned an unsupported device list.".into());
        }
        let mut parts = line.split_whitespace();
        if let (Some(serial), Some("device")) = (parts.next(), parts.next())
            && let Some(endpoint) = local_endpoint(serial)
        {
            endpoints.entry(endpoint).or_insert_with(|| serial.to_owned());
        }
    }
    if !header {
        return Err("ADB returned an unsupported device list.".into());
    }
    Ok(endpoints.into_values().collect())
}

#[derive(Debug)]
enum ProcessError {
    Spawn,
    Timeout,
    Output,
    Exit,
}

fn run_adb(path: &str, args: &[&str]) -> Result<Vec<u8>, ProcessError> {
    use std::io::Read;
    use std::process::{Command, Stdio};
    use std::sync::mpsc;
    use std::time::Instant;
    let mut command = Command::new(path);
    command.args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
    // ADB itself must remain local even if this desktop inherited alternate server settings.
    command
        .env_remove("ADB_SERVER_SOCKET")
        .env_remove("ANDROID_ADB_SERVER_ADDRESS")
        .env_remove("ANDROID_ADB_SERVER_PORT");
    command.env_remove("ADB_TRACE");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().map_err(|_| ProcessError::Spawn)?;
    let stdout = child.stdout.take().ok_or(ProcessError::Output)?;
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout.take((MAX_OUTPUT + 1) as u64).read_to_end(&mut bytes);
        let _ = sender.send(if result.is_ok() && bytes.len() <= MAX_OUTPUT {
            Ok(bytes)
        } else {
            Err(ProcessError::Output)
        });
    });
    let deadline = Instant::now() + COMMAND_TIMEOUT;
    let mut output = None;
    loop {
        match receiver.try_recv() {
            Ok(Ok(bytes)) => output = Some(bytes),
            Ok(Err(error)) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
            Err(mpsc::TryRecvError::Disconnected) if output.is_none() => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(ProcessError::Output);
            }
            _ => {}
        }
        match child.try_wait() {
            Ok(Some(status)) if !status.success() => return Err(ProcessError::Exit),
            Ok(Some(_)) if output.is_some() => return Ok(output.expect("output available")),
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(ProcessError::Exit);
            }
            _ => {}
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(ProcessError::Timeout);
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[derive(Default)]
struct Record {
    exact_package: bool,
    template: Option<bool>,
    call_type: Option<i32>,
    flags: Option<u32>,
    excluded_category: bool,
    call_hint: bool,
    malformed: bool,
    in_extras: bool,
    public_version: bool,
}

impl Record {
    fn from_header(header: &str) -> Self {
        let package = header.split_whitespace().find_map(|part| part.strip_prefix("pkg="));
        let mut record = Self { exact_package: package == Some(PACKAGE), ..Self::default() };
        record.read_category(header);
        // Android 14 includes hexadecimal flags in Notification.toString().
        if let Some(raw) = header.split_whitespace().find_map(|part| part.strip_prefix("flags=0x")) {
            record.flags = u32::from_str_radix(raw.trim_end_matches([')', ',']), 16).ok();
        }
        record
    }

    fn read_category(&mut self, line: &str) {
        for part in
            line.split(|character: char| character.is_whitespace() || matches!(character, '(' | ')' | ','))
        {
            if let Some(category) = part.strip_prefix("category=").or_else(|| part.strip_prefix("category:"))
            {
                let category = category.trim_end_matches([')', ',']);
                self.call_hint |= category == "call";
                self.excluded_category |= matches!(category, "msg" | "missed_call" | "voicemail" | "promo");
            }
        }
    }

    fn line(&mut self, line: &str) {
        if !self.exact_package {
            return;
        }
        let line = line.trim();
        if line == "publicNotification=" {
            self.public_version = true;
            self.in_extras = false;
        }
        if self.public_version {
            return;
        }
        self.read_category(line);
        if let Some(raw) = line.strip_prefix("flags=") {
            let parsed = if let Some(hex) = raw.strip_prefix("0x") {
                u32::from_str_radix(hex, 16).ok()
            } else if raw == "0" {
                Some(0)
            } else if raw.split('|').all(|flag| flag.starts_with("FLAG_") || flag == "0") {
                Some(if raw.split('|').any(|flag| flag == "FLAG_GROUP_SUMMARY") { 0x200 } else { 0 })
            } else {
                None
            };
            if let Some(flags) = parsed {
                // Never erase a group-summary bit already present in the record header.
                self.flags = Some(self.flags.unwrap_or_default() | flags);
            } else {
                self.malformed = true;
            }
        }
        if line == "extras={" {
            self.in_extras = true;
            return;
        }
        if line == "}" {
            self.in_extras = false;
            return;
        }
        if !self.in_extras {
            return;
        }
        if let Some(value) = line.strip_prefix("android.template=") {
            let template = value == "String (android.app.Notification$CallStyle)";
            if !template
                && !matches!(
                    value,
                    "String (android.app.Notification$MessagingStyle)"
                        | "String (android.app.Notification$BigTextStyle)"
                        | "String (android.app.Notification$BigPictureStyle)"
                        | "String (android.app.Notification$InboxStyle)"
                        | "String (android.app.Notification$MediaStyle)"
                )
            {
                self.malformed = true;
            }
            if self.template.replace(template).is_some() {
                self.malformed = true;
            }
        }
        if let Some(value) = line.strip_prefix("android.callType=") {
            self.call_hint = true;
            let parsed = value
                .strip_prefix("Integer (")
                .and_then(|v| v.strip_suffix(')'))
                .and_then(|v| v.parse::<i32>().ok());
            if parsed.is_none() || self.call_type.is_some() {
                self.malformed = true;
            }
            self.call_type = parsed;
        }
    }

    fn observation(&self) -> Observation {
        if !self.exact_package || self.excluded_category || self.flags.is_some_and(|flags| flags & 0x200 != 0)
        {
            return Observation::Idle;
        }
        if !self.malformed && self.template == Some(true) && self.flags.is_some() {
            match self.call_type {
                Some(1) => return Observation::Incoming,
                Some(2) => return Observation::Active,
                _ => {}
            }
        }
        if self.call_hint || self.template == Some(true) || self.malformed {
            Observation::Unsupported
        } else {
            // Custom/unknown notifications are not claimed to prove that Hushed is idle.
            if self.template == Some(false) { Observation::Idle } else { Observation::Unsupported }
        }
    }
}

fn parse_notifications(output: &str) -> Observation {
    if output.trim().is_empty() {
        return Observation::Unsupported;
    }
    let mut header = false;
    let mut record: Option<Record> = None;
    let mut result = Observation::Idle;
    let merge = |result: &mut Observation, next: Observation| {
        let priority = |value| match value {
            // A distinct incoming call may coexist with an ongoing-call notification.
            // Preserve the explicit ring instead of hiding call waiting behind active state.
            Observation::Incoming => 3,
            Observation::Active => 2,
            Observation::Unsupported => 1,
            _ => 0,
        };
        if priority(next) > priority(*result) {
            *result = next;
        }
    };
    for line in output.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if trimmed == "Notification List:" && !header {
            header = true;
            continue;
        }
        if !header {
            return Observation::Unsupported;
        }
        if trimmed.starts_with("NotificationRecord(") {
            if let Some(previous) = record.take() {
                merge(&mut result, previous.observation());
            }
            record = Some(Record::from_header(trimmed));
        } else if let Some(record) = record.as_mut() {
            // A legacy service that ignored --dump-priority must not mix archive records in.
            if line.len() - line.trim_start().len() <= 2 && trimmed.ends_with(':') {
                return Observation::Unsupported;
            }
            record.line(line);
        } else {
            return Observation::Unsupported;
        }
    }
    if let Some(previous) = record {
        merge(&mut result, previous.observation());
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn notification(package: &str, call_type: i32, flags: &str) -> String {
        format!(
            "  Notification List:\n    NotificationRecord(0x123: pkg={package} user=UserHandle{{0}} id=7 tag=null importance=4 key=PRIVATE: Notification(channel=call flags={flags}))\n      flags={flags}\n      extras={{\n        android.title=String [length=10]\n        android.template=String (android.app.Notification$CallStyle)\n        android.callType=Integer ({call_type})\n      }}\n"
        )
    }

    fn observe(sampler: &mut Sampler, serials: &[&str], states: &[Observation]) -> Sample {
        let selected: Vec<_> = serials.iter().map(|v| (*v).to_owned()).collect();
        sampler.apply(&selected, &selected.iter().cloned().zip(states.iter().copied()).collect::<Vec<_>>())
    }

    #[test]
    fn local_serials_reject_remote_shell_arguments_and_alias_duplicates() {
        for serial in [
            "192.168.1.5:5555",
            "localhost:5555",
            "127.0.0.1:0",
            "127.0.0.1:65536",
            "127.0.0.1:05555",
            "emulator-",
            "emulator-5554 shell",
            "emulator-65535",
            "-s",
            "[::1]:5555",
        ] {
            assert_eq!(local_endpoint(serial), None, "{serial}");
        }
        assert_eq!(local_endpoint("emulator-5554"), Some(5555));
        assert_eq!(local_endpoint("127.0.0.1:5555"), Some(5555));
        assert!(
            Config { serials: vec!["emulator-5554".into(), "127.0.0.1:5555".into()], ..Config::default() }
                .validate()
                .is_err()
        );
        assert!(
            Config {
                serials: (0..5).map(|n| format!("emulator-{}", 5554 + n * 2)).collect(),
                ..Config::default()
            }
            .validate()
            .is_err()
        );
        assert!(Config::default().validate().is_ok());
    }

    #[test]
    fn device_list_only_returns_connected_local_endpoints() {
        assert_eq!(parse_devices("List of devices attached\nemulator-5554\tdevice\n127.0.0.1:5555\tdevice\nemulator-5556\toffline\n192.168.1.2:5555\tdevice\nphysical-private-serial\tdevice\n127.0.0.1:5559\tdevice\n").unwrap(), vec!["emulator-5554", "127.0.0.1:5559"]);
        assert!(parse_devices("error: unavailable").is_err());
    }

    #[test]
    fn package_presence_does_not_confuse_shell_failure_with_not_installed() {
        assert_eq!(parse_package_list(b""), Some(false));
        assert_eq!(parse_package_list(b" \r\n"), Some(false));
        assert_eq!(parse_package_list(b"package:com.hushed.release\r\n"), Some(true));
        assert_eq!(
            parse_package_list(b"package:com.hushed.release.beta\npackage:example.com.hushed.release\n"),
            Some(false)
        );
        assert_eq!(
            parse_package_list(b"package:com.hushed.release.beta\npackage:com.hushed.release\n"),
            Some(true)
        );
        for malformed in [
            "error: device offline",
            "Permission Denial",
            "package:unexpected",
            "package:/data/app/com.hushed.release/base.apk",
            "package:com.hushed.release\nPermission Denial",
            "package:com.hushed.release..beta",
            "package:com.hushed.release beta",
            "package:com.unrelated.app",
        ] {
            assert_eq!(parse_package_list(malformed.as_bytes()), None, "{malformed}");
        }
        assert_eq!(parse_package_list(&[0xff]), None);
        let mut sampler = Sampler::default();
        let state = observe(&mut sampler, &["emulator-5556"], &[Observation::NotInstalled]);
        assert_eq!(state.status.state, "not_installed");
        assert_eq!(state.status.connected, 1);
        assert!(state.calls.is_empty());
    }

    #[test]
    fn native_android14_fixture_format_and_production_package_exclusion() {
        let cases = [
            (include_str!("../tests/fixtures/hushed/android14-incoming.txt"), Observation::Incoming),
            (include_str!("../tests/fixtures/hushed/android14-ongoing.txt"), Observation::Active),
            (include_str!("../tests/fixtures/hushed/android14-screening.txt"), Observation::Unsupported),
            (include_str!("../tests/fixtures/hushed/android14-message.txt"), Observation::Idle),
            (include_str!("../tests/fixtures/hushed/android14-missed.txt"), Observation::Idle),
            (include_str!("../tests/fixtures/hushed/android14-group.txt"), Observation::Idle),
        ];
        for (native, expected) in cases {
            // Real native framework notifications, from an entirely separate fictional app.
            assert_eq!(parse_notifications(native), Observation::Idle);
            // Test-only package normalization verifies parsing, never actual Hushed behavior.
            assert_eq!(
                parse_notifications(&native.replace("app.careerpilot.callfixture", PACKAGE)),
                expected
            );
        }
        assert_eq!(
            parse_notifications(include_str!("../tests/fixtures/hushed/android14-empty.txt")),
            Observation::Idle
        );
        assert_eq!(
            parse_notifications(include_str!("../tests/fixtures/hushed/android14-production-filter.txt")),
            Observation::Idle
        );
    }

    #[test]
    fn incoming_and_ongoing_require_exact_package_style_and_integer_type() {
        assert_eq!(parse_notifications(&notification(PACKAGE, 1, "0x2")), Observation::Incoming);
        assert_eq!(parse_notifications(&notification(PACKAGE, 2, "0x2")), Observation::Active);
        assert_eq!(
            parse_notifications(&notification("com.hushed.release.fake", 1, "0x2")),
            Observation::Idle
        );
        assert_eq!(parse_notifications(&notification(PACKAGE, 3, "0x2")), Observation::Unsupported);
        assert_eq!(
            parse_notifications(&notification(PACKAGE, 1, "0x2").replace("Integer (1)", "String (1)")),
            Observation::Unsupported
        );
        assert_eq!(
            parse_notifications(
                &notification(PACKAGE, 1, "0x2")
                    .replace("android.app.Notification$CallStyle", "android.app.Notification$MessagingStyle")
            ),
            Observation::Unsupported
        );
    }

    #[test]
    fn ignores_group_summaries_messages_and_missed_calls() {
        assert_eq!(parse_notifications(&notification(PACKAGE, 1, "0x202")), Observation::Idle);
        assert_eq!(
            parse_notifications(&notification(PACKAGE, 1, "FLAG_ONGOING_EVENT|FLAG_GROUP_SUMMARY")),
            Observation::Idle
        );
        for category in ["msg", "missed_call", "voicemail", "promo"] {
            assert_eq!(
                parse_notifications(
                    &notification(PACKAGE, 1, "0x2")
                        .replace("channel=call", &format!("category={category} channel=call"))
                ),
                Observation::Idle
            );
        }
    }

    #[test]
    fn unknown_format_is_not_idle_and_archives_are_not_calls() {
        assert_eq!(parse_notifications(""), Observation::Unsupported);
        assert_eq!(parse_notifications("  Notification List:\n"), Observation::Idle);
        assert_eq!(parse_notifications("Permission Denial: private details"), Observation::Unsupported);
        assert_eq!(
            parse_notifications(
                "Notification List:\n    NotificationRecord(0x123: pkg=com.hushed.release id=1)\n      category=call\n"
            ),
            Observation::Unsupported
        );
        assert_eq!(
            parse_notifications(&format!(
                "{}  Enqueued Notification List:\n{}",
                notification(PACKAGE, 1, "0x2"),
                notification(PACKAGE, 2, "0x2")
            )),
            Observation::Unsupported
        );
        assert_eq!(
            parse_notifications(
                &notification(PACKAGE, 1, "0x2")
                    .replace("      extras={", "      publicNotification=\n      extras={")
            ),
            Observation::Unsupported
        );
    }

    #[test]
    fn duplicate_or_missing_metadata_fails_closed() {
        let incoming = notification(PACKAGE, 1, "0x2");
        assert_eq!(
            parse_notifications(&incoming.replace(
                "android.callType=Integer (1)",
                "android.callType=Integer (1)\n        android.callType=Integer (2)"
            )),
            Observation::Unsupported
        );
        assert_eq!(
            parse_notifications(&incoming.replace(
                "android.template=String (android.app.Notification$CallStyle)",
                "android.template=String [length=34]"
            )),
            Observation::Unsupported
        );
        assert_eq!(parse_notifications(&incoming.replace("0x2", "unknown")), Observation::Unsupported);
        let custom = incoming
            .replace("android.app.Notification$CallStyle", "com.example.CustomCall")
            .replace("        android.callType=Integer (1)\n", "");
        assert_eq!(parse_notifications(&custom), Observation::Unsupported);
        assert_eq!(parse_notifications(&incoming.replace("flags=0x2))", "flags=0x202))")), Observation::Idle);
    }

    #[test]
    fn occurrence_survives_ringing_answer_notification_replacement_and_one_gap() {
        let mut sampler = Sampler::default();
        let first = observe(&mut sampler, &["emulator-5554"], &[Observation::Incoming]);
        assert!(first.calls[0].incoming);
        assert!(observe(&mut sampler, &["emulator-5554"], &[Observation::Idle]).retired.is_empty());
        let active = observe(&mut sampler, &["emulator-5554"], &[Observation::Active]);
        assert_eq!(first.calls[0].key, active.calls[0].key);
        assert!(!active.calls[0].incoming);
        assert!(!active.calls[0].key.contains("5554"));
        assert!(active.calls[0].key.ends_with(":Hushed"));
    }

    #[test]
    fn two_healthy_absences_allow_next_call_even_when_notification_id_is_reused() {
        let mut sampler = Sampler::default();
        let first = observe(&mut sampler, &["emulator-5554"], &[Observation::Incoming]);
        observe(&mut sampler, &["emulator-5554"], &[Observation::Idle]);
        let ended = observe(&mut sampler, &["emulator-5554"], &[Observation::Idle]);
        assert_eq!(ended.retired, vec![first.calls[0].key.clone()]);
        let next = observe(&mut sampler, &["emulator-5554"], &[Observation::Incoming]);
        assert_ne!(first.calls[0].key, next.calls[0].key);
    }

    #[test]
    fn new_ring_after_ongoing_call_gets_a_new_occurrence_without_an_idle_sample() {
        let mut sampler = Sampler::default();
        let first = observe(&mut sampler, &["emulator-5554"], &[Observation::Active]);
        let next = observe(&mut sampler, &["emulator-5554"], &[Observation::Incoming]);
        assert_eq!(next.retired, vec![first.calls[0].key.clone()]);
        assert_ne!(first.calls[0].key, next.calls[0].key);
        assert!(next.forgotten.is_empty());
    }

    #[test]
    fn explicit_incoming_takes_precedence_when_ongoing_and_incoming_coexist() {
        let incoming = notification(PACKAGE, 1, "0x2");
        let active = notification(PACKAGE, 2, "0x2");
        for (first, second) in [(&incoming, &active), (&active, &incoming)] {
            let records = format!("{first}{}", second.replacen("  Notification List:\n", "", 1));
            assert_eq!(parse_notifications(&records), Observation::Incoming);
        }
    }

    #[test]
    fn unknown_breaks_absence_streak_and_does_not_refresh_evidence() {
        let mut sampler = Sampler::default();
        let first = observe(&mut sampler, &["emulator-5554"], &[Observation::Active]);
        observe(&mut sampler, &["emulator-5554"], &[Observation::Idle]);
        for error in [Observation::Unavailable, Observation::Unsupported] {
            let failed = observe(&mut sampler, &["emulator-5554"], &[error]);
            assert!(failed.calls.is_empty());
            assert!(failed.retired.is_empty());
            assert!(observe(&mut sampler, &["emulator-5554"], &[Observation::Idle]).retired.is_empty());
        }
        let restored = observe(&mut sampler, &["emulator-5554"], &[Observation::Active]);
        assert_eq!(first.calls[0].key, restored.calls[0].key);
    }

    #[test]
    fn devices_are_independent_and_removing_or_disabling_forgets_keys() {
        let mut sampler = Sampler::default();
        let both = observe(
            &mut sampler,
            &["emulator-5554", "emulator-5556"],
            &[Observation::Incoming, Observation::Active],
        );
        assert_ne!(both.calls[0].key, both.calls[1].key);
        let partial = observe(
            &mut sampler,
            &["emulator-5554", "emulator-5556"],
            &[Observation::Unavailable, Observation::Active],
        );
        assert_eq!(partial.status.state, "partial");
        assert_eq!(partial.status.connected, 1);
        assert_eq!(partial.calls[0].key, both.calls[1].key);
        let removed = observe(&mut sampler, &["emulator-5556"], &[Observation::Active]);
        assert_eq!(removed.forgotten, vec![both.calls[0].key.clone()]);
        let disabled = sampler.sample(&Config::default());
        assert_eq!(disabled.forgotten, vec![both.calls[1].key.clone()]);
        assert_eq!(disabled.status.state, "disabled");
    }

    fn observe_telecom(sampler: &mut Sampler, epoch: &str, calls: &[(&str, telecom::Phase)]) -> Sample {
        sampler.apply_devices(
            &["emulator-5554".into()],
            &[(
                "emulator-5554".into(),
                DeviceObservation::Telecom {
                    epoch: epoch.into(),
                    calls: calls
                        .iter()
                        .map(|(id, phase)| telecom::Call { id: (*id).into(), phase: *phase })
                        .collect(),
                },
            )],
        )
    }

    #[test]
    fn telecom_ring_answer_hold_and_transition_keep_one_occurrence() {
        use telecom::Phase::*;
        let mut sampler = Sampler::default();
        let first = observe_telecom(&mut sampler, "boot:11", &[("TC@1", Incoming)]);
        for phase in [Active, Active, Transitional, Active] {
            let next = observe_telecom(&mut sampler, "boot:11", &[("TC@1", phase)]);
            assert!(next.retired.is_empty());
            assert!(next.forgotten.is_empty());
            if phase == Transitional {
                assert!(next.calls.is_empty());
            } else {
                assert_eq!(first.calls[0].key, next.calls[0].key);
            }
        }
        assert!(!first.calls[0].key.contains("TC@"));
        assert!(!first.calls[0].key.contains("5554"));
        assert!(!first.calls[0].key.contains("boot"));
    }

    #[test]
    fn telecom_consecutive_and_simultaneous_calls_have_independent_keys() {
        use telecom::Phase::*;
        let mut sampler = Sampler::default();
        let first = observe_telecom(&mut sampler, "boot:11", &[("TC@1", Active)]);
        let second = observe_telecom(&mut sampler, "boot:11", &[("TC@2", Active)]);
        assert_ne!(first.calls[0].key, second.calls[0].key);
        assert!(second.retired.is_empty());
        let both = observe_telecom(&mut sampler, "boot:11", &[("TC@2", Active), ("TC@3", Incoming)]);
        assert_eq!(both.calls[0].key, second.calls[0].key);
        assert_ne!(both.calls[0].key, both.calls[1].key);
        assert_eq!(both.retired, vec![first.calls[0].key.clone()]);
        assert_eq!(both.status.state, "incoming");
    }

    #[test]
    fn telecom_unknown_breaks_absence_and_ended_needs_two_healthy_samples() {
        use telecom::Phase::*;
        let mut sampler = Sampler::default();
        let first = observe_telecom(&mut sampler, "boot:11", &[("TC@1", Active)]);
        assert!(observe_telecom(&mut sampler, "boot:11", &[]).retired.is_empty());
        for state in [Observation::Unsupported, Observation::Unavailable, Observation::NotInstalled] {
            let failed = sampler.apply_devices(
                &["emulator-5554".into()],
                &[("emulator-5554".into(), DeviceObservation::Unknown(state))],
            );
            assert!(failed.calls.is_empty());
            assert!(failed.retired.is_empty());
            assert!(observe_telecom(&mut sampler, "boot:11", &[("TC@1", Ended)]).retired.is_empty());
        }
        let ended = observe_telecom(&mut sampler, "boot:11", &[]);
        assert_eq!(ended.retired, vec![first.calls[0].key.clone()]);
        let next = observe_telecom(&mut sampler, "boot:11", &[("TC@1", Incoming)]);
        assert_ne!(next.calls[0].key, first.calls[0].key);
    }

    #[test]
    fn telecom_restart_forgets_old_identity_without_claiming_call_end() {
        use telecom::Phase::*;
        let mut sampler = Sampler::default();
        let first = observe_telecom(&mut sampler, "boot:11", &[("TC@1", Active)]);
        let restart = observe_telecom(&mut sampler, "boot:12", &[("TC@1", Active)]);
        assert_eq!(restart.forgotten, vec![first.calls[0].key.clone()]);
        assert!(restart.retired.is_empty());
        assert_ne!(restart.calls[0].key, first.calls[0].key);
        let reboot = observe_telecom(&mut sampler, "reboot:12", &[("TC@1", Active)]);
        assert_eq!(reboot.forgotten, vec![restart.calls[0].key.clone()]);
        let removed = sampler.apply_devices(&[], &[]);
        assert_eq!(removed.forgotten, vec![reboot.calls[0].key.clone()]);
    }

    #[test]
    fn notification_to_telecom_handoff_preserves_dismissal_identity_across_one_gap() {
        use telecom::Phase::*;
        let mut sampler = Sampler::default();
        let first = observe(&mut sampler, &["emulator-5554"], &[Observation::Incoming]);
        assert!(observe_telecom(&mut sampler, "boot:11", &[]).retired.is_empty());
        let answered = observe_telecom(&mut sampler, "boot:11", &[("TC@1", Active)]);
        assert_eq!(first.calls[0].key, answered.calls[0].key);
        assert!(answered.forgotten.is_empty());
        assert!(answered.retired.is_empty());
        let disabled = sampler.sample(&Config::default());
        assert_eq!(disabled.forgotten, vec![first.calls[0].key.clone()]);
    }

    #[test]
    fn telecom_transition_without_supported_call_phase_never_prompts() {
        let mut sampler = Sampler::default();
        let pending = observe_telecom(&mut sampler, "boot:11", &[("TC@1", telecom::Phase::Transitional)]);
        assert!(pending.calls.is_empty());
        assert!(pending.retired.is_empty());
        assert!(pending.block_end_review);
        let unknown = sampler.apply_devices(
            &["emulator-5554".into()],
            &[("emulator-5554".into(), DeviceObservation::Unknown(Observation::Unavailable))],
        );
        assert!(unknown.block_end_review);
        assert!(unknown.calls.is_empty());
        let ended = observe_telecom(&mut sampler, "boot:11", &[("TC@1", telecom::Phase::Ended)]);
        assert!(ended.calls.is_empty());
        assert!(ended.retired.is_empty());
        assert!(ended.block_end_review);
        let retired = observe_telecom(&mut sampler, "boot:11", &[]);
        assert_eq!(retired.retired.len(), 1);
        assert!(retired.block_end_review);
        assert!(!observe_telecom(&mut sampler, "boot:11", &[]).block_end_review);
    }

    #[test]
    fn process_identity_accepts_only_one_pid_and_canonical_boot_shape() {
        assert_eq!(parse_pid(b"123\r\n"), Some("123".into()));
        for invalid in ["", "0", "-1", "1 2", "Permission Denial", "4294967296"] {
            assert!(parse_pid(invalid.as_bytes()).is_none());
        }
        assert_eq!(
            parse_boot_id(b"ABCDEF01-2345-6789-ABCD-0123456789AB\n"),
            Some("abcdef01-2345-6789-abcd-0123456789ab".into())
        );
        for invalid in ["", "abcdef01-2345-6789-abcd-0123456789ag", "abcdef0123456789abcd0123456789abcd"] {
            assert!(parse_boot_id(invalid.as_bytes()).is_none());
        }
    }

    #[test]
    fn subprocess_deadline_stops_a_hung_client() {
        let executable = std::env::current_exe().expect("test executable");
        let started = Instant::now();
        let result = run_adb(
            executable.to_str().expect("UTF-8 test path"),
            &["--exact", "background::hushed::tests::subprocess_timeout_fixture", "--ignored", "--nocapture"],
        );
        assert!(matches!(result, Err(ProcessError::Timeout)));
        assert!(started.elapsed() < Duration::from_secs(6), "hung child outlived bounded runner");
    }

    #[test]
    fn subprocess_output_limit_stops_an_excessive_client() {
        let executable = std::env::current_exe().expect("test executable");
        let started = Instant::now();
        let result = run_adb(
            executable.to_str().expect("UTF-8 test path"),
            &["--exact", "background::hushed::tests::subprocess_output_fixture", "--ignored", "--nocapture"],
        );
        assert!(matches!(result, Err(ProcessError::Output)));
        assert!(started.elapsed() < Duration::from_secs(6), "excessive child outlived bounded runner");
    }

    #[test]
    #[ignore = "child process fixture; invoked only by subprocess_deadline_stops_a_hung_client"]
    fn subprocess_timeout_fixture() {
        std::thread::sleep(Duration::from_secs(10));
    }

    #[test]
    #[ignore = "child process fixture; invoked only by subprocess_output_limit_stops_an_excessive_client"]
    fn subprocess_output_fixture() {
        use std::io::Write;
        let _ = std::io::stdout().lock().write_all(&vec![b'x'; MAX_OUTPUT * 2]);
    }

    #[test]
    #[ignore = "read-only validation emulator check; requires explicit CAREER_PILOT_HUSHED_TEST_ADB and CAREER_PILOT_HUSHED_TEST_SERIAL"]
    fn native_validation_emulator_without_hushed_reports_not_installed() {
        let adb_path = std::env::var("CAREER_PILOT_HUSHED_TEST_ADB").expect("explicit validation ADB path");
        let serial =
            std::env::var("CAREER_PILOT_HUSHED_TEST_SERIAL").expect("explicit validation emulator serial");
        let config = Config { enabled: true, adb_path, serials: vec![serial.clone()] };
        config.validate().expect("valid local validation device configuration");
        let listed = devices(&config.adb_path).expect("list local validation devices");
        assert!(listed.iter().any(|value| local_endpoint(value) == local_endpoint(&serial)));
        // Assert absence before sampling: this test never reads any real Hushed notification.
        let package = run_adb(&config.adb_path, &["-s", &serial, "shell", "pm", "list", "packages", PACKAGE])
            .expect("query filtered package presence");
        assert_eq!(parse_package_list(&package), Some(false), "use a validation emulator without Hushed");
        let sample = Sampler::default().sample(&config);
        assert_eq!(sample.status.state, "not_installed");
        assert_eq!(sample.status.connected, 1);
        assert_eq!(sample.status.monitored, 1);
        assert!(sample.calls.is_empty());
        assert!(sample.retired.is_empty());
        assert!(sample.forgotten.is_empty());
    }
}
