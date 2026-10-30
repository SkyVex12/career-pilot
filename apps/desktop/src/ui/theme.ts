// The colour theme: the OS setting, or the user's override (Settings, General). The choice is kept
// per computer in localStorage, which both windows share, so a change in one shows in the other
// (the storage event). The resolved theme is set on <html data-theme> before the first paint.
import { useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";
export const THEME_KEY = "copilot.theme";

export function loadTheme(store: Pick<Storage, "getItem"> | null = safeStorage()): ThemeChoice {
  try {
    const v = store?.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function resolveTheme(choice: ThemeChoice, systemDark: boolean): "light" | "dark" {
  return choice === "system" ? (systemDark ? "dark" : "light") : choice;
}

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function systemDark(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function applyTheme(choice: ThemeChoice = loadTheme()): void {
  document.documentElement.dataset.theme = resolveTheme(choice, systemDark());
}

/** Applies the theme now and keeps it applied: OS changes, and changes made in the other window. */
export function startTheme(): void {
  applyTheme();
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => applyTheme());
  window.addEventListener("storage", (e) => {
    if (e.key === THEME_KEY) applyTheme();
  });
}

export function useTheme(): [ThemeChoice, (c: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(() => loadTheme());
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_KEY) setChoice(loadTheme());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  const set = (c: ThemeChoice) => {
    setChoice(c);
    try {
      if (c === "system") window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, c);
    } catch {
      // a convenience only
    }
    applyTheme(c);
  };
  return [choice, set];
}
