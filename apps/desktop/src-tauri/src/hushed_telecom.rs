//! Reads only current, structured Telecom state. Raw dump fields never leave this module.
//!
//! Android 14's CallsManager.dump prints mCalls before its other state; the surrounding
//! TelecomServiceImpl.dump prints PhoneAccountRegistrar next and Analytics afterwards.
//! PhoneAccountRegistrar.dump uses phoneAccounts, followed by its test-filter marker.
//! History and account labels/extras are not call or registration evidence.

use std::collections::HashSet;

const COMPONENT: &str = "com.hushed.release/com.hushed.base.telephony.HushedConnectionService";
const MAX_DUMP_BYTES: usize = 256 * 1024;
const MAX_LINE_BYTES: usize = 16 * 1024;
const MAX_CALLS: usize = 32;
const MAX_ACCOUNTS: usize = 128;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum Snapshot {
    Unknown,
    Unregistered,
    Registered(Vec<Call>),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Call {
    pub id: String,
    pub phase: Phase,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Phase {
    Incoming,
    Active,
    Transitional,
    Ended,
}

#[derive(Clone, Copy)]
struct Line<'a> {
    indent: usize,
    text: &'a str,
}

pub(super) fn parse(input: &str) -> Snapshot {
    parse_complete(input).unwrap_or(Snapshot::Unknown)
}

fn parse_complete(input: &str) -> Option<Snapshot> {
    if input.len() > MAX_DUMP_BYTES || input.contains('\0') {
        return None;
    }
    let mut lines = Vec::new();
    for raw in input.lines() {
        if raw.len() > MAX_LINE_BYTES {
            return None;
        }
        let trimmed = raw.trim_end();
        if trimmed.is_empty() {
            continue;
        }
        let indent = trimmed.bytes().take_while(|byte| *byte == b' ').count();
        let text = &trimmed[indent..];
        if text.starts_with(char::is_whitespace) {
            return None;
        }
        lines.push(Line { indent, text });
        // Android's subsequent historical event log uses a different indentation
        // format (including tabs). Only this boundary is needed; history is never
        // validated or interpreted as current call/account evidence.
        if indent == 0 && text == "Analytics:" {
            break;
        }
    }

    if lines.first().is_none_or(|line| line.indent != 0 || line.text != "CallsManager:") {
        return None;
    }
    let registrar = (1..lines.len()).find(|&index| lines[index].indent == 0)?;
    if lines[registrar].text != "PhoneAccountRegistrar:" {
        return None;
    }
    let analytics = (registrar + 1..lines.len()).find(|&index| lines[index].indent == 0)?;
    if lines[analytics].text != "Analytics:" {
        return None;
    }

    let calls_header = lines.get(1)?;
    let unit = calls_header.indent;
    if calls_header.text != "mCalls:" || !(1..=8).contains(&unit) {
        return None;
    }
    if lines[2..registrar].iter().any(|line| line.text == "mCalls:") {
        return None;
    }
    let mut calls = Vec::new();
    let mut ids = HashSet::new();
    for line in &lines[2..registrar] {
        if line.indent <= unit {
            // A direct row with missing indentation must not turn into a healthy
            // empty snapshot. AOSP siblings are m-prefixed fields, not call rows.
            if line.indent != unit || !is_calls_sibling(line.text) {
                return None;
            }
            break;
        }
        if line.indent != unit * 2 || ids.len() >= MAX_CALLS {
            return None;
        }
        let (id, owned, state) = parse_call_row(line.text)?;
        if !ids.insert(id) {
            return None;
        }
        if owned {
            calls.push(Call { id: id.to_owned(), phase: parse_phase(state)? });
        }
    }

    // An explicit, completed registrar section is required even when mCalls is empty.
    // This distinguishes a healthy idle provider from an unsupported/truncated dump.
    let mut account_header = None;
    for (index, line) in lines.iter().enumerate().take(analytics).skip(registrar + 1) {
        if line.text == "phoneAccounts:" && (line.indent != unit || account_header.replace(index).is_some()) {
            return None;
        }
    }
    let account_header = account_header?;
    let mut registered = false;
    let mut accounts_complete = false;
    for (account_count, line) in lines[account_header + 1..analytics].iter().enumerate() {
        if line.indent == unit * 2 && line.text.starts_with("test emergency PhoneAccount filter:") {
            accounts_complete = true;
            break;
        }
        if line.indent != unit * 2 || account_count >= MAX_ACCOUNTS {
            return None;
        }
        registered |= parse_account_row(line.text)?;
    }
    if !accounts_complete {
        return None;
    }

    if registered || !calls.is_empty() {
        Some(Snapshot::Registered(calls))
    } else {
        Some(Snapshot::Unregistered)
    }
}

fn is_calls_sibling(text: &str) -> bool {
    let Some((name, _)) = text.split_once(':') else {
        return false;
    };
    name.starts_with('m') && name.len() > 1 && name.bytes().all(|byte| byte.is_ascii_alphanumeric())
}

fn parse_call_row(text: &str) -> Option<(&str, bool, &str)> {
    let row = text.strip_prefix("[Call id=")?;
    let (id, row) = row.split_once(", state=")?;
    let digits = id.strip_prefix("TC@")?;
    if digits.is_empty() || digits.len() > 10 || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let (state, target) = row.split_once(", tpac=")?;
    if state.is_empty() || !state.bytes().all(|byte| byte.is_ascii_uppercase() || byte == b'_') {
        return None;
    }
    let body = target.strip_suffix("], voip=true").or_else(|| target.strip_suffix("], voip=false"))?;
    // Validate the formatter's field order without interpreting caller/account data.
    let (_, fields) = body.split_once(", cmgr=")?;
    let (_, fields) = fields.split_once(", handle=")?;
    let (_, fields) = fields.split_once(", vidst=")?;
    let (_, fields) = fields.split_once(", childs(")?;
    let (children, fields) = fields.split_once("), has_parent(")?;
    if children.is_empty() || !children.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let fields = fields.strip_prefix("true), cap=").or_else(|| fields.strip_prefix("false), cap="))?;
    let (_, properties) = fields.split_once(", prop=")?;
    if properties.is_empty() {
        return None;
    }
    let owned = if let Some(component) = target.strip_prefix("ComponentInfo{") {
        let (component, remainder) = component.split_once('}')?;
        if !remainder.starts_with(", ") || !remainder.contains(", cmgr=") {
            return None;
        }
        component == COMPONENT
    } else if target.starts_with("null, cmgr=") {
        false
    } else {
        return None;
    };
    Some((id, owned, state))
}

fn parse_phase(state: &str) -> Option<Phase> {
    match state {
        "RINGING" => Some(Phase::Incoming),
        "ACTIVE" | "ON_HOLD" => Some(Phase::Active),
        "NEW"
        | "CONNECTING"
        | "SELECT_PHONE_ACCOUNT"
        | "DIALING"
        | "DISCONNECTING"
        | "PULLING"
        | "ANSWERED"
        | "AUDIO_PROCESSING"
        | "SIMULATED_RINGING" => Some(Phase::Transitional),
        "DISCONNECTED" | "ABORTED" => Some(Phase::Ended),
        _ => None,
    }
}

fn parse_account_row(text: &str) -> Option<bool> {
    let (enabled, row) = if let Some(row) = text.strip_prefix("[[X] PhoneAccount: ") {
        (true, row)
    } else {
        (false, text.strip_prefix("[[ ] PhoneAccount: ")?)
    };
    let row = row.strip_prefix("ComponentInfo{")?;
    let (component, remainder) = row.split_once('}')?;
    if !remainder.starts_with(", ") || !remainder.ends_with(']') {
        return None;
    }
    let (_, capabilities) = remainder.split_once(" Capabilities: ")?;
    let (capabilities, _) = capabilities.split_once(" Audio Routes: ")?;
    Some(enabled && component == COMPONENT && capabilities.split_whitespace().any(|c| c == "SelfManaged"))
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE_COMPONENT: &str =
        "app.careerpilot.telecomfixture/app.careerpilot.telecomfixture.FixtureConnectionService";

    // Source-based fixtures follow AOSP android-14.0.0_r1 Call.toString,
    // PhoneAccount.toString, and their dump sections. All addresses/account IDs are synthetic.
    fn call(id: &str, state: &str, component: &str) -> String {
        format!(
            "    [Call id={id}, state={state}, tpac=ComponentInfo{{{component}}}, ***N***, UserHandle{{0}}, cmgr=null, handle=***N***, vidst=A, childs(0), has_parent(false), cap=[], prop=[self_mng]], voip=true\n"
        )
    }

    fn account(component: &str, enabled: bool, capabilities: &str) -> String {
        let enabled = if enabled { 'X' } else { ' ' };
        format!(
            "    [[{enabled}] PhoneAccount: ComponentInfo{{{component}}}, ***N***, UserHandle{{0}} Capabilities: {capabilities} Audio Routes: BESW Schemes: tel Extras: null GroupId: ***N***]\n"
        )
    }

    fn dump(calls: &str, accounts: &str) -> String {
        format!(
            "CallsManager:\n  mCalls:\n{calls}  mCallAudioManager:\n    foregroundCall: null\nPhoneAccountRegistrar:\n  xmlVersion: 9\n  defaultOutgoing: none\n  outgoingPhoneAccountForTelScheme: none\n  simCallManager: null\n  phoneAccounts:\n{accounts}    test emergency PhoneAccount filter: null\nAnalytics:\n  historical data is not evidence\n"
        )
    }

    fn registered(calls: &str) -> String {
        dump(calls, &account(COMPONENT, true, "SelfManaged "))
    }

    #[test]
    fn registered_idle_does_not_need_a_notification_or_microphone() {
        assert_eq!(parse(&registered("")), Snapshot::Registered(vec![]));
        assert_eq!(parse(&dump("", "")), Snapshot::Unregistered);
    }

    #[test]
    fn native_android14_idle_fixtures_validate_framing_and_owner_isolation() {
        let empty = include_str!("../tests/fixtures/hushed/telecom-android14-empty.txt");
        let registered = include_str!("../tests/fixtures/hushed/telecom-android14-registered-idle.txt");
        assert_eq!(parse(empty), Snapshot::Unregistered);
        assert_eq!(parse(registered), Snapshot::Unregistered);
        // Only the fictional provider identity is changed, inside this test. The
        // captured idle fixture proves framing/registration, never a live call.
        assert!(registered.contains(FIXTURE_COMPONENT));
        assert_eq!(parse(&registered.replace(FIXTURE_COMPONENT, COMPONENT)), Snapshot::Registered(vec![]));
    }

    #[test]
    fn source_based_lifecycle_fixtures_require_exact_provider_ownership() {
        // These lifecycle rows are synthetic AOSP-format examples, not native
        // Hushed observations. Native active lifecycle validation is separate.
        for (input, phase) in [
            (include_str!("../tests/fixtures/hushed/telecom-synthetic-ringing.txt"), Phase::Incoming),
            (include_str!("../tests/fixtures/hushed/telecom-synthetic-active.txt"), Phase::Active),
            (include_str!("../tests/fixtures/hushed/telecom-synthetic-held.txt"), Phase::Active),
            (include_str!("../tests/fixtures/hushed/telecom-synthetic-disconnected.txt"), Phase::Ended),
        ] {
            assert!(input.contains(FIXTURE_COMPONENT));
            assert_eq!(parse(input), Snapshot::Unregistered);
            assert_eq!(
                parse(&input.replace(FIXTURE_COMPONENT, COMPONENT)),
                Snapshot::Registered(vec![Call { id: "TC@1".into(), phase }])
            );
        }
    }

    #[test]
    fn current_telecom_states_distinguish_ringing_active_hold_and_end() {
        for (state, phase) in [
            ("RINGING", Phase::Incoming),
            ("ACTIVE", Phase::Active),
            ("ON_HOLD", Phase::Active),
            ("DISCONNECTED", Phase::Ended),
            ("ABORTED", Phase::Ended),
        ] {
            assert_eq!(
                parse(&registered(&call("TC@17", state, COMPONENT))),
                Snapshot::Registered(vec![Call { id: "TC@17".into(), phase }]),
                "{state}"
            );
        }
    }

    #[test]
    fn intermediate_states_preserve_identity_without_claiming_ringing() {
        for state in [
            "NEW",
            "CONNECTING",
            "SELECT_PHONE_ACCOUNT",
            "DIALING",
            "DISCONNECTING",
            "PULLING",
            "ANSWERED",
            "AUDIO_PROCESSING",
            "SIMULATED_RINGING",
        ] {
            assert_eq!(
                parse(&registered(&call("TC@4", state, COMPONENT))),
                Snapshot::Registered(vec![Call { id: "TC@4".into(), phase: Phase::Transitional }]),
                "{state}"
            );
        }
    }

    #[test]
    fn matching_current_call_is_evidence_even_if_account_was_unregistered() {
        assert_eq!(
            parse(&dump(&call("TC@2", "ACTIVE", COMPONENT), "")),
            Snapshot::Registered(vec![Call { id: "TC@2".into(), phase: Phase::Active }])
        );
    }

    #[test]
    fn multiple_current_calls_keep_separate_opaque_ids() {
        let calls = call("TC@8", "ON_HOLD", COMPONENT) + &call("TC@9", "RINGING", COMPONENT);
        assert_eq!(
            parse(&registered(&calls)),
            Snapshot::Registered(vec![
                Call { id: "TC@8".into(), phase: Phase::Active },
                Call { id: "TC@9".into(), phase: Phase::Incoming },
            ])
        );
    }

    #[test]
    fn history_and_other_current_sections_cannot_create_call_evidence() {
        let historical = call("TC@91", "RINGING", COMPONENT);
        let input = registered("")
            .replace("    foregroundCall: null", &format!("    foregroundCall: null\n{historical}"))
            + &historical;
        assert_eq!(parse(&input), Snapshot::Registered(vec![]));
    }

    #[test]
    fn tab_indented_history_does_not_invalidate_completed_current_sections() {
        // Android 14 emits tab-indented historical events after Analytics. This
        // structure is independent of account identities and caller content.
        let history = "\nHistorical Events:\n\tTC@91:\n\t\tstate=RINGING\n";
        let input = registered("") + history + &call("TC@91", "RINGING", COMPONENT);
        assert_eq!(parse(&input), Snapshot::Registered(vec![]));
        assert_eq!(parse(&(dump("", "") + history)), Snapshot::Unregistered);

        // Tabs within the authoritative section still fail closed.
        let invalid = registered(&call("TC@1", "RINGING", COMPONENT)).replacen("    [Call", "\t[Call", 1);
        assert_eq!(parse(&invalid), Snapshot::Unknown);
    }

    #[test]
    fn package_mentions_outside_account_component_do_not_register_provider() {
        let input = dump("", &account("other.app/other.Service", true, "SelfManaged "))
            .replace("defaultOutgoing: none", &format!("defaultOutgoing: ComponentInfo{{{COMPONENT}}}"))
            .replace("Extras: null", &format!("Extras: Bundle[ComponentInfo{{{COMPONENT}}}]"))
            .replace("PhoneAccount filter: null", &format!("PhoneAccount filter: {COMPONENT}"));
        assert_eq!(parse(&input), Snapshot::Unregistered);
    }

    #[test]
    fn exact_component_and_enabled_self_managed_account_are_required() {
        for other in [
            "com.hushed.release.evil/com.hushed.base.telephony.HushedConnectionService",
            "com.hushed.release/com.hushed.base.telephony.HushedConnectionServiceOther",
            "com.hushed.release/other.Service",
        ] {
            assert_eq!(
                parse(&dump(&call("TC@3", "RINGING", other), &account(other, true, "SelfManaged "))),
                Snapshot::Unregistered
            );
        }
        assert_eq!(parse(&dump("", &account(COMPONENT, false, "SelfManaged "))), Snapshot::Unregistered);
        assert_eq!(parse(&dump("", &account(COMPONENT, true, "CallProvider "))), Snapshot::Unregistered);
    }

    #[test]
    fn connection_manager_and_caller_fields_cannot_spoof_the_target() {
        let other = call("TC@3", "RINGING", "other.app/other.Service")
            .replace("cmgr=null", &format!("cmgr=ComponentInfo{{{COMPONENT}}}"))
            .replace("handle=***N***", &format!("handle={COMPONENT}"));
        assert_eq!(parse(&dump(&other, "")), Snapshot::Unregistered);
    }

    #[test]
    fn partial_missing_or_duplicate_sections_are_unknown_not_idle() {
        let complete = registered("");
        for input in [
            String::new(),
            "Permission Denial: can't dump TelecomService".into(),
            "CallsManager:\n  mCalls:\n".into(),
            complete.split("PhoneAccountRegistrar:").next().unwrap().into(),
            complete.split("Analytics:").next().unwrap().into(),
            complete.replace("  phoneAccounts:\n", ""),
            complete.replace("  phoneAccounts:\n", "  phoneAccounts:\n  phoneAccounts:\n"),
            complete.replace("  mCalls:\n", "  mCalls:\n  mCalls:\n"),
            complete.replace("    test emergency PhoneAccount filter: null\n", ""),
            complete.replace("PhoneAccountRegistrar:", "History:"),
        ] {
            assert_eq!(parse(&input), Snapshot::Unknown);
        }
    }

    #[test]
    fn malformed_current_rows_and_unknown_hushed_states_are_unknown() {
        for row in [
            call("TC@1", "FUTURE_STATE", COMPONENT),
            call("caller-name", "RINGING", COMPONENT),
            call("TC@", "RINGING", COMPONENT),
            call("TC@12345678901", "RINGING", COMPONENT),
            call("TC@1", "RINGING", COMPONENT).replace("], voip=true", ""),
            call("TC@1", "RINGING", COMPONENT).replacen("    ", "      ", 1),
            call("TC@1", "RINGING", COMPONENT).replacen("    ", "  ", 1),
            call("TC@1", "RINGING", COMPONENT).replace(", handle=***N***", ""),
            call("TC@1", "RINGING", COMPONENT).replace("childs(0)", "childs(x)"),
            call("TC@1", "RINGING", COMPONENT).replace("has_parent(false)", "has_parent(unknown)"),
            "    unrecognized call row\n".into(),
        ] {
            assert_eq!(parse(&registered(&row)), Snapshot::Unknown);
        }
    }

    #[test]
    fn duplicate_current_call_ids_are_rejected() {
        let rows = call("TC@1", "RINGING", COMPONENT) + &call("TC@1", "ACTIVE", COMPONENT);
        assert_eq!(parse(&registered(&rows)), Snapshot::Unknown);
    }

    #[test]
    fn raw_private_fields_are_not_returned() {
        let input = registered(&call("TC@6", "RINGING", COMPONENT)).replace("***N***", "PRIVATE_TEST_CANARY");
        let parsed = parse(&input);
        assert_eq!(parsed, Snapshot::Registered(vec![Call { id: "TC@6".into(), phase: Phase::Incoming }]));
        assert!(!format!("{parsed:?}").contains("PRIVATE_TEST_CANARY"));
    }

    #[test]
    fn parsing_is_bounded_and_accepts_crlf() {
        assert_eq!(parse(&registered("").replace('\n', "\r\n")), Snapshot::Registered(vec![]));
        assert_eq!(parse(&"x".repeat(MAX_DUMP_BYTES + 1)), Snapshot::Unknown);
        assert_eq!(
            parse(&registered("").replace("  mCallAudioManager:", &"x".repeat(MAX_LINE_BYTES + 1))),
            Snapshot::Unknown
        );
        assert_eq!(parse(&(registered("") + "\0")), Snapshot::Unknown);
        let rows: String = (0..=MAX_CALLS).map(|n| call(&format!("TC@{n}"), "ACTIVE", COMPONENT)).collect();
        assert_eq!(parse(&registered(&rows)), Snapshot::Unknown);
    }
}
