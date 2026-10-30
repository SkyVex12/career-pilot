// Hotkeys in the UI: recording one from a key press (Settings), reading one ("Ctrl+Shift+G",
// the overlay and Settings), and finding two actions on one key. Stored in Tauri's accelerator
// form, as before (src-tauri/src/hotkeys.rs parses it and re-checks every saved hotkey).
import type { HotkeyAction, Settings } from "./api";

export const HOTKEYS: { key: keyof Settings; action: HotkeyAction; label: string; hint?: string; fallback?: string }[] = [
  { key: "hotkey", action: "force_question", label: "Answer the last thing said", hint: "When detection missed a question." },
  { key: "toggle_overlay_hotkey", action: "toggle_overlay", label: "Hide or show the overlay", hint: "The call keeps running." },
  { key: "click_through_hotkey", action: "click_through", label: "Click-through on or off", hint: "Clicks pass to the window underneath." },
  {
    key: "click_through_off_hotkey",
    action: "click_through_off",
    label: "Turn click-through off",
    hint: "The way back. It always works: cleared, it falls back to Ctrl+Shift+G.",
    fallback: "CommandOrControl+Shift+G",
  },
  { key: "opacity_up_hotkey", action: "opacity_up", label: "Overlay more opaque" },
  { key: "opacity_down_hotkey", action: "opacity_down", label: "Overlay more see-through" },
];

const KEY_LABELS: Record<string, string> = {
  CommandOrControl: "Ctrl",
  CommandOrCtrl: "Ctrl",
  CmdOrCtrl: "Ctrl",
  CmdOrControl: "Ctrl",
  Control: "Ctrl",
  Command: "Cmd",
  Super: "Win",
  Up: "↑",
  Down: "↓",
  Left: "←",
  Right: "→",
};

/** A readable chip for an accelerator: "CommandOrControl+Shift+H" -> "Ctrl+Shift+H". */
export function prettyKey(accel: string): string {
  return accel
    .split("+")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => KEY_LABELS[p] ?? p)
    .join("+");
}

const CODE_KEYS: Record<string, string> = {
  Space: "Space",
  Enter: "Enter",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Delete",
  Insert: "Insert",
  Home: "Home",
  End: "End",
  PageUp: "PageUp",
  PageDown: "PageDown",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Minus: "-",
  Equal: "=",
  BracketLeft: "[",
  BracketRight: "]",
  Backslash: "\\",
  Semicolon: ";",
  Quote: "'",
  Comma: ",",
  Period: ".",
  Slash: "/",
  Backquote: "`",
};

/** The accelerator a key press records, or a reason it cannot be one. Modifiers alone wait
 * for a key; a plain key needs a modifier (Ctrl, Alt, Shift or Win), except F1–F24. */
export function recordKey(e: { code: string; key: string; ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean }):
  | { accel: string }
  | { waiting: true }
  | { error: string } {
  if (["ControlLeft", "ControlRight", "ShiftLeft", "ShiftRight", "AltLeft", "AltRight", "MetaLeft", "MetaRight"].includes(e.code)) return { waiting: true };
  let key: string | null = null;
  if (/^Key[A-Z]$/.test(e.code)) key = e.code.slice(3);
  else if (/^Digit[0-9]$/.test(e.code)) key = e.code.slice(5);
  else if (/^Numpad[0-9]$/.test(e.code)) key = `Num${e.code.slice(6)}`;
  else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(e.code)) key = e.code;
  else key = CODE_KEYS[e.code] ?? null;
  if (!key) return { error: `That key (${e.key}) can't be used. Try a letter, a number or an F key.` };
  const mods = [e.ctrlKey && "CommandOrControl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Super"].filter(Boolean) as string[];
  if (!mods.length && !/^F\d+$/.test(key)) return { error: "Add Ctrl, Alt, Shift or Win, so typing never triggers it." };
  return { accel: [...mods, key].join("+") };
}

/** A rough normalization matching the Rust parser (hotkeys.rs), to find duplicates as the user
 * records. The saved settings are also checked on the Rust side (view.hotkey_review). */
export function normAccel(a: string): string | null {
  const parts = a.trim().split("+").map((p) => p.trim().toUpperCase()).filter(Boolean);
  if (!parts.length) return null;
  const mods = new Set<string>();
  let key = "";
  for (const p of parts) {
    if (["CONTROL", "CTRL", "COMMANDORCONTROL", "COMMANDORCTRL", "CMDORCTRL", "CMDORCONTROL"].includes(p)) mods.add("CTRL");
    else if (["COMMAND", "CMD", "SUPER"].includes(p)) mods.add("SUPER");
    else if (["ALT", "OPTION"].includes(p)) mods.add("ALT");
    else if (p === "SHIFT") mods.add("SHIFT");
    else key = p;
  }
  if (!key) return null;
  return [...[...mods].sort(), key].join("+");
}

/** The action already on this accelerator, other than `except`. */
export function clashWith(settings: Settings, accel: string, except: keyof Settings): (typeof HOTKEYS)[number] | null {
  const n = normAccel(accel);
  if (!n) return null;
  return HOTKEYS.find((h) => h.key !== except && normAccel(String(settings[h.key] ?? "")) === n) ?? null;
}

/** The settings keys that share a hotkey with another. */
export function duplicateHotkeys(settings: Settings): Set<keyof Settings> {
  const seen = new Map<string, (keyof Settings)[]>();
  for (const { key } of HOTKEYS) {
    const n = normAccel(String(settings[key] ?? ""));
    if (!n) continue;
    seen.set(n, [...(seen.get(n) ?? []), key]);
  }
  const dup = new Set<keyof Settings>();
  for (const keys of seen.values()) if (keys.length > 1) keys.forEach((k) => dup.add(k));
  return dup;
}
