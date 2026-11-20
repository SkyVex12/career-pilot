//! Opt-in, local Android notification metadata. Never captures audio or starts a call.
//!
//! AOSP's redacted dump can still contain action labels and object descriptions. Raw
//! output is bounded, held only while parsing, and never returned in diagnostics.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::time::{Duration, Instant};

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
}

#[derive(Default)]
pub(super) struct Sampler {
    occurrences: BTreeMap<String, Tracked>,
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
            forgotten = std::mem::take(&mut self.occurrences).into_values().map(|value| value.key).collect();
            self.installed.clear();
            self.adb_path.clone_from(&config.adb_path);
        }
        self.installed.retain(|serial, _| config.serials.contains(serial));
        // At most two bounded commands per instance, concurrently across all four instances.
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
                    scope.spawn(move || {
                        let installed = match cached {
                            Some(value) => Ok(Some(value)),
                            None => run_adb(
                                &config.adb_path,
                                &["-s", serial.as_str(), "shell", "pm", "path", PACKAGE],
                            )
                            .map(|bytes| parse_package_path(&bytes)),
                        };
                        let installed = match installed {
                            Ok(Some(value)) => value,
                            _ => return (serial.clone(), Observation::Unavailable, None),
                        };
                        if !installed {
                            return (serial.clone(), Observation::NotInstalled, Some(false));
                        }
                        let args = [
                            "-s",
                            serial.as_str(),
                            "shell",
                            "dumpsys",
                            "notification",
                            "--dump-priority",
                            "CRITICAL",
                            "--package",
                            PACKAGE,
                        ];
                        let observation = match run_adb(&config.adb_path, &args) {
                            Ok(bytes) => std::str::from_utf8(&bytes)
                                .map(parse_notifications)
                                .unwrap_or(Observation::Unsupported),
                            Err(_) => Observation::Unavailable,
                        };
                        (serial.clone(), observation, Some(true))
                    })
                })
                .collect();
            handles
                .into_iter()
                .zip(&config.serials)
                .map(|(handle, serial)| {
                    handle.join().unwrap_or_else(|_| (serial.clone(), Observation::Unavailable, None))
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
        let mut result = self.apply(&config.serials, &states);
        result.forgotten.extend(forgotten);
        result
    }

    fn clear(&mut self, state: &str, message: &str) -> Sample {
        let forgotten = std::mem::take(&mut self.occurrences).into_values().map(|v| v.key).collect();
        self.installed.clear();
        Sample {
            forgotten,
            status: Status { state: state.into(), message: message.into(), ..Status::default() },
            ..Sample::default()
        }
    }

    fn apply(&mut self, selected: &[String], observations: &[(String, Observation)]) -> Sample {
        let mut result = Sample::default();
        self.occurrences.retain(|serial, tracked| {
            if selected.contains(serial) {
                true
            } else {
                result.forgotten.push(tracked.key.clone());
                false
            }
        });
        let mut unsupported = 0;
        let mut unavailable = 0;
        let mut missing_package = 0;
        for (serial, observation) in observations {
            match observation {
                Observation::Incoming | Observation::Active => {
                    // A fresh ring after an ongoing call represents a new incoming occurrence,
                    // even if polling missed the short interval with no notification.
                    if *observation == Observation::Incoming
                        && self.occurrences.get(serial).is_some_and(|call| !call.incoming)
                        && let Some(previous) = self.occurrences.remove(serial)
                    {
                        result.retired.push(previous.key);
                    }
                    if !self.occurrences.contains_key(serial) {
                        self.sequence = self.sequence.wrapping_add(1);
                        self.occurrences.insert(
                            serial.clone(),
                            Tracked {
                                // No caller, notification tag/key, package data or emulator name.
                                key: format!("android-{}:Hushed", self.sequence),
                                missing: 0,
                                incoming: *observation == Observation::Incoming,
                            },
                        );
                    }
                    let tracked = self.occurrences.get_mut(serial).expect("inserted occurrence");
                    tracked.missing = 0;
                    tracked.incoming = *observation == Observation::Incoming;
                    result.calls.push(Call {
                        key: tracked.key.clone(),
                        incoming: *observation == Observation::Incoming,
                    });
                }
                Observation::Idle => {
                    if let Some(tracked) = self.occurrences.get_mut(serial) {
                        tracked.missing += 1;
                        if tracked.missing >= 2 {
                            result.retired.push(tracked.key.clone());
                            self.occurrences.remove(serial);
                        }
                    }
                }
                Observation::Unsupported | Observation::Unavailable | Observation::NotInstalled => {
                    // Unknown is not proof of a call ending; it breaks consecutive absence.
                    if let Some(tracked) = self.occurrences.get_mut(serial) {
                        tracked.missing = 0;
                    }
                    if *observation == Observation::Unsupported {
                        unsupported += 1;
                    } else if *observation == Observation::Unavailable {
                        unavailable += 1;
                    } else {
                        missing_package += 1;
                    }
                }
            }
        }
        let connected = observations.len().saturating_sub(unavailable);
        let (state, message) = if unavailable + unsupported + missing_package > 0
            && connected > unsupported + missing_package
        {
            (
                "partial",
                "Some emulators are unavailable, lack Hushed, or use an unsupported notification format.",
            )
        } else if unsupported > 0 {
            (
                "unsupported",
                "Hushed notification format is not supported on this emulator. Start assistance manually.",
            )
        } else if missing_package > 0 {
            ("not_installed", "Hushed is not installed on the selected local emulator.")
        } else if unavailable > 0 {
            ("unavailable", "Local emulator connection unavailable. Check ADB and the selected emulator.")
        } else if result.calls.iter().any(|call| !call.incoming) {
            ("active", "Hushed ongoing-call notification detected.")
        } else if !result.calls.is_empty() {
            ("incoming", "Hushed incoming-call notification detected.")
        } else {
            (
                "idle",
                "Monitoring local Hushed notifications. Real Hushed call behavior still needs verification.",
            )
        };
        result.status =
            Status { state: state.into(), message: message.into(), connected, monitored: selected.len() };
        result
    }
}

fn parse_package_path(bytes: &[u8]) -> Option<bool> {
    let text = std::str::from_utf8(bytes).ok()?.trim();
    if text.is_empty() {
        return Some(false);
    }
    if text.lines().all(|line| line.starts_with("package:/") && line.ends_with(".apk")) {
        Some(true)
    } else {
        None
    }
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
        assert_eq!(parse_package_path(b""), Some(false));
        assert_eq!(
            parse_package_path(
                b"package:/data/app/fictional/base.apk\npackage:/data/app/fictional/split.apk\n"
            ),
            Some(true)
        );
        assert_eq!(parse_package_path(b"error: device offline"), None);
        assert_eq!(parse_package_path(b"Permission Denial"), None);
        assert_eq!(parse_package_path(b"package:unexpected"), None);
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
        let package = run_adb(&config.adb_path, &["-s", &serial, "shell", "pm", "path", PACKAGE])
            .expect("query exact package presence");
        assert_eq!(parse_package_path(&package), Some(false), "use a validation emulator without Hushed");
        let sample = Sampler::default().sample(&config);
        assert_eq!(sample.status.state, "not_installed");
        assert_eq!(sample.status.connected, 1);
        assert_eq!(sample.status.monitored, 1);
        assert!(sample.calls.is_empty());
        assert!(sample.retired.is_empty());
        assert!(sample.forgotten.is_empty());
    }
}
