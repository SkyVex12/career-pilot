//! The overlay's global hotkeys: the set of actions a key can be bound to, parsing and
//! validating the accelerators, finding clashes between them, and the safety rule that the
//! click-through "off" switch always has a working key. The registration itself lives in
//! lib.rs (it needs the running app); everything here is pure so it can be tested.

use crate::settings::Settings;
use serde::Serialize;
use std::str::FromStr;
use tauri_plugin_global_shortcut::Shortcut;

/// Everything a global hotkey can do. `ClickThroughOff` is the escape hatch: while
/// click-through is on, the mouse cannot reach the app, so a key must always turn it back off.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    ForceQuestion,
    ToggleOverlay,
    ClickThrough,
    ClickThroughOff,
    OpacityUp,
    OpacityDown,
}

/// The key the click-through "off" switch falls back to when the user has cleared or mistyped
/// its binding, so there is never a state where click-through is on and no key turns it off.
pub const CLICK_THROUGH_OFF_FALLBACK: &str = "CommandOrControl+Shift+G";

/// Each action and the accelerator the user has bound to it (trimmed). An empty string means
/// the action is unbound, which is allowed for every action except the click-through off
/// switch, whose binding is resolved to a working key here.
pub fn bindings(s: &Settings) -> Vec<(Action, String)> {
    vec![
        (Action::ForceQuestion, s.hotkey.trim().to_string()),
        (Action::ToggleOverlay, s.toggle_overlay_hotkey.trim().to_string()),
        (Action::ClickThrough, s.click_through_hotkey.trim().to_string()),
        (Action::ClickThroughOff, click_through_off(s)),
        (Action::OpacityUp, s.opacity_up_hotkey.trim().to_string()),
        (Action::OpacityDown, s.opacity_down_hotkey.trim().to_string()),
    ]
}

/// The click-through off accelerator, guaranteed to be a key that parses: the user's if it is
/// valid, otherwise the fallback.
pub fn click_through_off(s: &Settings) -> String {
    let raw = s.click_through_off_hotkey.trim();
    if !raw.is_empty() && Shortcut::from_str(raw).is_ok() {
        raw.to_string()
    } else {
        CLICK_THROUGH_OFF_FALLBACK.to_string()
    }
}

/// Parse one accelerator, mapping an empty field and a parse error to a message for the UI.
pub fn parse(accelerator: &str) -> Result<Shortcut, String> {
    let a = accelerator.trim();
    if a.is_empty() {
        return Err("no key set".into());
    }
    Shortcut::from_str(a).map_err(|e| e.to_string())
}

#[derive(Debug, Clone, Serialize)]
pub struct InvalidBinding {
    pub action: Action,
    pub accelerator: String,
    pub error: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Conflict {
    pub accelerator: String,
    pub actions: Vec<Action>,
}

/// What the settings screen needs to warn about: accelerators that do not parse, and the
/// groups of actions that share one key.
#[derive(Debug, Default, Clone, Serialize)]
pub struct Review {
    pub invalid: Vec<InvalidBinding>,
    pub conflicts: Vec<Conflict>,
}

pub fn review(s: &Settings) -> Review {
    let mut invalid = Vec::new();
    let mut parsed: Vec<(Action, Shortcut, String)> = Vec::new();
    for (action, accel) in bindings(s) {
        if accel.is_empty() {
            continue; // Unbound is not a conflict and not an error.
        }
        match Shortcut::from_str(&accel) {
            Ok(sc) => parsed.push((action, sc, accel)),
            Err(e) => invalid.push(InvalidBinding { action, accelerator: accel, error: e.to_string() }),
        }
    }

    // Two spellings of the same combo parse to equal shortcuts, so equality finds every clash.
    let mut conflicts = Vec::new();
    let mut taken = vec![false; parsed.len()];
    for i in 0..parsed.len() {
        if taken[i] {
            continue;
        }
        let mut actions = vec![parsed[i].0];
        for j in (i + 1)..parsed.len() {
            if !taken[j] && parsed[j].1 == parsed[i].1 {
                taken[j] = true;
                actions.push(parsed[j].0);
            }
        }
        if actions.len() > 1 {
            conflicts.push(Conflict { accelerator: parsed[i].2.clone(), actions });
        }
    }
    Review { invalid, conflicts }
}

/// The shortcuts to actually register, one action per distinct key. On a clash the
/// click-through off switch wins, so turning click-through off never loses its key to another
/// binding; otherwise the action listed earlier in `bindings` keeps the key. Invalid and unbound
/// accelerators are dropped. The off switch is always present (its binding is resolved to a
/// working key), so there is always a way back from click-through.
pub fn to_register(s: &Settings) -> Vec<(Shortcut, Action)> {
    let mut out: Vec<(Shortcut, Action)> = Vec::new();
    for (action, accel) in bindings(s) {
        let Ok(sc) = parse(&accel) else { continue };
        match out.iter_mut().find(|(k, _)| *k == sc) {
            Some(slot) => {
                if action == Action::ClickThroughOff {
                    slot.1 = Action::ClickThroughOff;
                }
            }
            None => out.push((sc, action)),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> Settings {
        Settings::default()
    }

    #[test]
    fn defaults_parse_and_do_not_clash() {
        let s = settings();
        let r = review(&s);
        assert!(r.invalid.is_empty(), "defaults should all parse: {:?}", r.invalid);
        assert!(r.conflicts.is_empty(), "defaults should not clash: {:?}", r.conflicts);
    }

    #[test]
    fn a_shared_key_is_reported_as_one_conflict() {
        let mut s = settings();
        s.toggle_overlay_hotkey = "CommandOrControl+Shift+Space".into(); // same as force_question
        let r = review(&s);
        assert_eq!(r.conflicts.len(), 1);
        let c = &r.conflicts[0];
        assert!(c.actions.contains(&Action::ForceQuestion) && c.actions.contains(&Action::ToggleOverlay));
    }

    #[test]
    fn different_spellings_of_one_combo_still_clash() {
        let mut s = settings();
        s.hotkey = "Ctrl+Shift+J".into();
        s.opacity_up_hotkey = "Control+Shift+J".into();
        let r = review(&s);
        assert_eq!(r.conflicts.len(), 1, "same combo, different spelling, is one conflict");
    }

    #[test]
    fn a_nonsense_key_is_invalid_but_an_empty_one_is_not() {
        let mut s = settings();
        s.hotkey = "Ctrl+Shift+NotAKey".into();
        s.toggle_overlay_hotkey = "   ".into(); // cleared on purpose: unbound, not an error
        let r = review(&s);
        assert_eq!(r.invalid.len(), 1);
        assert_eq!(r.invalid[0].action, Action::ForceQuestion);
    }

    #[test]
    fn click_through_off_always_resolves_to_a_working_key() {
        // Cleared, whitespace, or garbage all fall back to a key that parses.
        for cleared in ["", "   ", "Ctrl+Shift+Nope"] {
            let mut s = settings();
            s.click_through_off_hotkey = cleared.into();
            let off = click_through_off(&s);
            assert!(parse(&off).is_ok(), "off must be a valid key for input {cleared:?}");
        }
    }

    #[test]
    fn the_off_switch_is_always_registered_even_on_a_clash() {
        // Bind every other action, and the off switch, to the very same key.
        let mut s = settings();
        let same = "CommandOrControl+Shift+Space";
        s.hotkey = same.into();
        s.toggle_overlay_hotkey = same.into();
        s.click_through_hotkey = same.into();
        s.click_through_off_hotkey = same.into();
        s.opacity_up_hotkey = same.into();
        s.opacity_down_hotkey = same.into();
        let reg = to_register(&s);
        // One key, and it drives the off switch: click-through can always be escaped.
        assert_eq!(reg.len(), 1);
        assert_eq!(reg[0].1, Action::ClickThroughOff);

        // And with the off switch cleared, the fallback key registers it on its own.
        let mut s = settings();
        s.click_through_off_hotkey = "".into();
        let reg = to_register(&s);
        assert!(reg.iter().any(|(_, a)| *a == Action::ClickThroughOff));
    }
}
