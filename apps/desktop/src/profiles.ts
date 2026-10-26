// The profile switch (ARCHITECTURE.md, "Profiles"): "Me" (the account) and one profile per
// Career-OS user an admin link reads. The chosen profile is kept by the core (settings.profile),
// which names it on every API call and session; every screen reads and writes that profile's
// applications, resumes, interviews, sessions and work. The app remounts the screens on a switch.
import { createContext, useCallback, useEffect, useState } from "react";
import { api, rest, type Profile } from "./api";

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

/** The switch shows when there is someone besides "Me". */
export function showSwitch(profiles: Profile[]): boolean {
  return profiles.some((p) => !p.me);
}

export function useProfiles(ready: boolean, saved: string | null) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [current, setCurrent] = useState<string | null>(saved);
  useEffect(() => setCurrent(saved), [saved]);

  const reload = useCallback(async () => {
    if (!ready) return;
    try {
      const out = await rest.profiles();
      setProfiles(out.items);
      if (isStale(out.items, current)) {
        await api.setProfile(null);
        setCurrent(null);
      }
    } catch {
      // A profile this account no longer has makes every call fail: back to "Me".
      if (current !== null) {
        await api.setProfile(null).catch(() => undefined);
        setCurrent(null);
      }
    }
  }, [ready, current]);

  useEffect(() => {
    reload();
  }, [ready]); // eslint-disable-line react-hooks/exhaustive-deps

  const switchTo = async (id: string | null) => {
    const me = profiles.find((p) => p.me);
    const next = id === null || id === me?.id ? null : id;
    await api.setProfile(next);
    setCurrent(next);
  };

  const state: ProfileState = { profiles, current, here: hereOf(profiles, current) };
  return { state, reload, switchTo };
}
