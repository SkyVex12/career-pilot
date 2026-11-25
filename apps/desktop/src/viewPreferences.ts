import { useCallback, useEffect, useSyncExternalStore } from "react";
import type { AnswerStyle, ResponseLength } from "@copilot/protocol";
import { api } from "./api";
import { LAYOUT_KEY, TEXT_SCALE_KEY, loadLayout, loadTextScale, savePref, stepScale, type OverlayLayout } from "./transcript";

export const PREFERENCE_EVENT = "copilot:preferences";
const ANSWER_STYLE_KEY = "copilot.answerStyle";
const RESPONSE_LENGTH_KEY = "copilot.responseLength";
export const RESPONSE_LENGTHS = [["default", "Default"], ["concise", "Concise"], ["detailed", "Detailed"]] as const;
function subscribe(notify: () => void) {
  window.addEventListener("storage", notify);
  window.addEventListener(PREFERENCE_EVENT, notify);
  return () => { window.removeEventListener("storage", notify); window.removeEventListener(PREFERENCE_EVENT, notify); };
}

/** One reading preference across components, windows and History replay. */
export function useViewPrefs() {
  const textScale = useSyncExternalStore(subscribe, loadTextScale);
  const layout = useSyncExternalStore(subscribe, loadLayout);
  const stepText = useCallback((steps: number) => savePref(TEXT_SCALE_KEY, String(steps === 0 ? 1 : stepScale(loadTextScale(), steps))), []);
  const setLayout = useCallback((value: OverlayLayout) => savePref(LAYOUT_KEY, value), []);
  return { textScale, layout, stepText, setLayout };
}
export type ViewPrefs = ReturnType<typeof useViewPrefs>;

function loadAnswerStyle(): AnswerStyle {
  try { return localStorage.getItem(ANSWER_STYLE_KEY) === "paragraph" ? "paragraph" : "bullets"; }
  catch { return "bullets"; }
}
function loadResponseLength(): ResponseLength {
  try { const value = localStorage.getItem(RESPONSE_LENGTH_KEY); return value === "concise" || value === "detailed" ? value : "default"; }
  catch { return "default"; }
}
let answerPreferenceChange: Promise<void> = Promise.resolve();
function changeAnswerPreference(running: boolean, key: string, value: AnswerStyle | ResponseLength) {
  const apply = async () => {
    // Read the other setting inside the lock, so two windows cannot overwrite a newer choice.
    const style = key === ANSWER_STYLE_KEY ? value as AnswerStyle : loadAnswerStyle();
    const length = key === RESPONSE_LENGTH_KEY ? value as ResponseLength : loadResponseLength();
    if (running) await api.setAnswerStyle(style, length);
    savePref(key, value);
  };
  const change = () => navigator.locks ? navigator.locks.request("copilot.answerPreferences", apply) : apply();
  const pending = answerPreferenceChange.then(change, change);
  answerPreferenceChange = pending.catch(() => undefined);
  return pending;
}
export function useAnswerStyle(running: boolean) {
  const value = useSyncExternalStore(subscribe, loadAnswerStyle);
  const change = useCallback((next: AnswerStyle) => changeAnswerPreference(running, ANSWER_STYLE_KEY, next), [running]);
  return [value, change] as const;
}
export function useResponseLength(running: boolean) {
  const value = useSyncExternalStore(subscribe, loadResponseLength);
  const change = useCallback((next: ResponseLength) => changeAnswerPreference(running, RESPONSE_LENGTH_KEY, next), [running]);
  return [value, change] as const;
}

/// The step Ctrl plus a key asks of the text size: +1, -1, 0 for the default, or null.
export function textKeyStep(e: { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean }): number | null {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return null;
  if (e.key === "=" || e.key === "+") return 1;
  if (e.key === "-" || e.key === "_") return -1;
  if (e.key === "0") return 0;
  return null;
}

/** Install once in each window root, so keyboard and menu controls share the same value. */
export function useTextScaleKeys(stepText: (steps: number) => void) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const steps = textKeyStep(event);
      if (steps === null || event.defaultPrevented) return;
      event.preventDefault();
      stepText(steps);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stepText]);
}
