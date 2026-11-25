import { useCallback, useSyncExternalStore } from "react";
import type { AnswerStyle } from "@copilot/protocol";
import { api } from "./api";
import { LAYOUT_KEY, TEXT_SCALE_KEY, loadLayout, loadTextScale, savePref, stepScale, type OverlayLayout } from "./transcript";

export const PREFERENCE_EVENT = "copilot:preferences";
const ANSWER_STYLE_KEY = "copilot.answerStyle";
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
export function useAnswerStyle(running: boolean) {
  const value = useSyncExternalStore(subscribe, loadAnswerStyle);
  const change = useCallback(async (next: AnswerStyle) => {
    if (running) await api.setAnswerStyle(next);
    savePref(ANSWER_STYLE_KEY, next);
  }, [running]);
  return [value, change] as const;
}
