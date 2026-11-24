// The profile switch (ARCHITECTURE.md, "Profiles"): "Me" (the account) and one profile per
// Career-OS user an admin link reads. The chosen profile is kept by the core (settings.profile),
// which names it on every API call and session; every screen reads and writes that profile's
// applications, resumes, interviews, sessions and work. The app remounts the screens on a switch,
// and on a sign-in to another account.
import { createContext, useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type CareerOsConnection, type Profile, type SettingsView } from "./api";
import { useDataActivity } from "./usePolling";

export interface ProfileState {
  profiles: Profile[];
  /** settings.profile: the profile chosen, null for "Me". */
  current: string | null;
  /** The id of the profile the app acts for ("Me": the account's id); null until loaded. */
  here: string | null;
}

export const ProfileContext = createContext<ProfileState>({ profiles: [], current: null, here: null });

/** The profile id the app acts for: the chosen one, else "Me". */
export function hereOf(profiles: Profile[], current: string | null): string | null {
  return current ?? profiles.find((p) => p.me)?.id ?? null;
}

/** A chosen profile that is no longer listed (unlinked in Career-OS, another account): back to "Me". */
export function isStale(profiles: Profile[], current: string | null): boolean {
  return current !== null && profiles.length > 0 && !profiles.some((p) => !p.me && p.id === current);
}

/** UI state is scoped to the configured deployment as well as the signed-in account.
 * The cloud URL retains its host placeholder, so a VM restart keeps the same identity. */
export function accountOf(view: SettingsView | null): string {
  if (!view) return "";
  const origin = view.settings.environment === "cloud" ? view.settings.cloud_api_url : view.settings.api_url;
  return JSON.stringify([view.settings.environment, origin?.trim().replace(/\/$/, "") ?? "", view.signed_in_as ?? ""]);
}

/** Locked until a Career-OS link: the stage requires one and the account has no active link. */
export function isLocked(conn: CareerOsConnection | null): boolean {
  return conn?.required === true && !conn.connected;
}

/** An API error that means the chosen profile is not this account's (any more). */
export function refusesProfile(error: string): boolean {
  return /\(HTTP 40[34]\)/.test(error);
}

/** The switch shows when there is someone besides "Me". */
export function showSwitch(profiles: Profile[]): boolean {
  return profiles.some((p) => !p.me);
}

export function useProfiles(ready: boolean, saved: string | null, account: string, onChange?: () => void) {
  const visible = useDataActivity(ready);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [current, setCurrent] = useState<string | null>(saved);
  const currentRef = useRef(current);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const applyCurrent = useCallback((next: string | null) => {
    if (next === currentRef.current) return;
    // Drop the old profile's call/preset before remounting its screens with the new profile.
    onChangeRef.current?.();
    currentRef.current = next;
    setCurrent(next);
  }, []);
  useEffect(() => applyCurrent(saved), [saved, applyCurrent]);
  // Bumped on every account change, so a list loaded for the previous account is dropped.
  const generation = useRef(0);

  const reload = useCallback(async () => {
    if (!visible) return;
    const gen = generation.current;
    try {
      const out = await rest.profiles();
      if (gen !== generation.current) return;
      setProfiles(out.items);
      if (isStale(out.items, current)) {
        await api.setProfile(null);
        applyCurrent(null);
      }
    } catch (e) {
      if (gen !== generation.current) return;
      // A profile this account no longer has is refused (403 or 404): back to "Me". A slow
      // or failed call (Career-OS, a restart, the network) keeps the profile and the list, so
      // the screens do not switch to someone else's data; the next load fixes it.
      if (!refusesProfile(String(e))) return;
      setProfiles([]);
      if (current !== null) {
        try {
          await api.setProfile(null);
          applyCurrent(null);
        } catch { /* A running call keeps its profile. */ }
      }
    }
  }, [visible, current, applyCurrent]);

  // Signed out, or another account signed in: forget the old account's profiles, then load.
  useEffect(() => {
    generation.current += 1;
    setProfiles([]);
  }, [ready, account]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (visible) void reload(); }, [visible, ready, account]); // eslint-disable-line react-hooks/exhaustive-deps

  const switchTo = async (id: string | null) => {
    const me = profiles.find((p) => p.me);
    const next = id === null || id === me?.id ? null : id;
    await api.setProfile(next);
    applyCurrent(next);
  };

  const state: ProfileState = { profiles, current, here: hereOf(profiles, current) };
  return { state, reload, switchTo };
}
