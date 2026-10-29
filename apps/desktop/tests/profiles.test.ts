// The profile switch's logic (src/profiles.ts).
import { describe, expect, it } from "vitest";
import type { CareerOsConnection, Profile, SettingsView } from "../src/api";
import { accountOf, hereOf, isLocked, isStale, showSwitch } from "../src/profiles";

const me: Profile = { id: "acct", name: "Dana", career_os_user_id: null, me: true };
const alex: Profile = { id: "p1", name: "Alex Doe", career_os_user_id: "u1", me: false };

describe("profiles", () => {
  it("acts for the chosen profile, else Me", () => {
    expect(hereOf([me, alex], "p1")).toBe("p1");
    expect(hereOf([me, alex], null)).toBe("acct");
    expect(hereOf([], null)).toBeNull();
  });

  it("drops a profile that is no longer listed", () => {
    expect(isStale([me, alex], "p1")).toBe(false);
    expect(isStale([me], "p1")).toBe(true);
    expect(isStale([me, alex], null)).toBe(false);
    // Not loaded yet: nothing to compare with.
    expect(isStale([], "p1")).toBe(false);
    // Me's id is never a chosen profile.
    expect(isStale([me, alex], "acct")).toBe(true);
  });

  it("shows the switch only when there is someone besides Me", () => {
    expect(showSwitch([me])).toBe(false);
    expect(showSwitch([me, alex])).toBe(true);
  });
  it("tells accounts apart, so a sign-in to another one reloads every screen", () => {
    const view = (environment: string, signed_in_as: string | null) =>
      ({ settings: { environment }, signed_in_as }) as unknown as SettingsView;
    expect(accountOf(null)).toBe("");
    expect(accountOf(view("cloud", "a@x.com"))).not.toBe(accountOf(view("cloud", "b@x.com")));
    expect(accountOf(view("cloud", null))).not.toBe(accountOf(view("cloud", "a@x.com")));
    expect(accountOf(view("local", null))).not.toBe(accountOf(view("cloud", null)));
  });
  it("locks the app only when the stage requires a link and there is none", () => {
    const conn = (over: Partial<CareerOsConnection>): CareerOsConnection => ({ connected: false, available: true, ...over });
    expect(isLocked(null)).toBe(false);
    expect(isLocked(conn({}))).toBe(false);
    expect(isLocked(conn({ required: true }))).toBe(true);
    expect(isLocked(conn({ required: true, status: "revoked" }))).toBe(true);
    expect(isLocked(conn({ required: true, connected: true, status: "active" }))).toBe(false);
  });
});
