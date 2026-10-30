// The profile switch (ARCHITECTURE.md, "Profiles"): "Me" (the account) and one profile per
// Career-OS user an admin link reads. The chosen profile is kept by the core (settings.profile),
// which names it on every API call and session; every screen reads and writes that profile's
// applications, resumes, interviews, sessions and work. The app remounts the screens on a switch,
// and on a sign-in to another account.
import { createContext, useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type CareerOsConnection, type Profile, type SettingsView } from "./api";

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

/** The signed-in account: the environment and, in the cloud, the email signed in with. */
export function accountOf(view: SettingsView | null): string {
  if (!view) return "";
  return `${view.settings.environment}:${view.signed_in_as ?? ""}`;
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

export function useProfiles(ready: boolean, saved: string | null, account: string) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [current, setCurrent] = useState<string | null>(saved);
  useEffect(() => setCurrent(saved), [saved]);
  // Bumped on every account change, so a list loaded for the previous account is dropped.
  const generation = useRef(0);

  const reload = useCallback(async () => {
    if (!ready) return;
    const gen = generation.current;
    try {
      const out = await rest.profiles();
      if (gen !== generation.current) return;
      setProfiles(out.items);
      if (isStale(out.items, current)) {
        await api.setProfile(null);
        setCurrent(null);
      }
    } catch (e) {
      if (gen !== generation.current) return;
      // A profile this account no longer has is refused (403 or 404): back to "Me". A slow
      // or failed call (Career-OS, a restart, the network) keeps the profile and the list, so
      // the screens do not switch to someone else's data; the next load fixes it.
      if (!refusesProfile(String(e))) return;
      setProfiles([]);
      if (current !== null) {
        await api.setProfile(null).catch(() => undefined);
        setCurrent(null);
      }
    }
  }, [ready, current]);

  // Signed out, or another account signed in: forget the old account's profiles, then load.
  useEffect(() => {
    generation.current += 1;
    setProfiles([]);
    reload();
  }, [ready, account]); // eslint-disable-line react-hooks/exhaustive-deps

  const switchTo = async (id: string | null) => {
    const me = profiles.find((p) => p.me);
    const next = id === null || id === me?.id ? null : id;
    await api.setProfile(next);
    setCurrent(next);
  };

  const state: ProfileState = { profiles, current, here: hereOf(profiles, current) };
  return { state, reload, switchTo };
}
