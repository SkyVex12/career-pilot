import { mockWindows } from "@tauri-apps/api/mocks";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Shell, useWorkspaceCallBusy } from "../src/Shell";
import { initial } from "../src/session";
import type { ProfileState } from "../src/profiles";

const profiles: ProfileState = {
  current: null, here: "me",
  profiles: [
    { id: "me", name: "Dana", me: true, career_os_user_id: null },
    { id: "p1", name: "Alex Doe", me: false, career_os_user_id: "alex" },
  ],
};
function Busy({ busy }: { busy: boolean }) { useWorkspaceCallBusy(busy); return null; }
function view(onSwitch = vi.fn(), state = profiles, busy = false, error: string | null = null) {
  mockWindows("main");
  return <Shell tab="start" onTab={() => {}} session={initial} profiles={state} onSwitchProfile={onSwitch} profileError={error} version={null} showDebug={false}>
    <Busy busy={busy} />
  </Shell>;
}

describe("header profile switch", () => {
  it("uses the existing switching callback and retains the current profile on failure", async () => {
    const change = vi.fn();
    const { rerender } = render(view(change));
    const profile = screen.getByRole("combobox", { name: "Profile" });
    expect(profile).toHaveTextContent("DA");
    await userEvent.click(profile);
    await userEvent.click(screen.getByRole("option", { name: "Alex Doe" }));
    expect(change).toHaveBeenCalledWith("p1");
    rerender(view(change, profiles, false, "Could not switch profiles"));
    expect(profile).toHaveTextContent("DA");
    expect(screen.getByRole("alert")).toHaveTextContent("Could not switch profiles");
  });
  it("prevents changing profiles while starting a call", async () => {
    const change = vi.fn();
    render(view(change, profiles, true));
    const profile = screen.getByRole("combobox", { name: "Profile" });
    expect(profile).toBeDisabled();
    await userEvent.click(profile);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });
  it("shows unavailable profiles without inventing an account", () => {
    render(view(vi.fn(), { profiles: [], here: null, current: null }));
    const profile = screen.getByRole("combobox", { name: "Profile" });
    expect(profile).toBeDisabled();
    expect(profile).toHaveTextContent("?");
  });
});
