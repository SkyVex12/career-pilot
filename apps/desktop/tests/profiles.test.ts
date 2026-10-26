// The profile switch's logic (src/profiles.ts).
import { describe, expect, it } from "vitest";
import type { Profile } from "../src/api";
import { hereOf, isStale, showSwitch } from "../src/profiles";

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
});
