import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SettingsScreen } from "../src/SettingsScreen";
import { ToastProvider } from "../src/ui";
import { SETTINGS, settingsView } from "./fixtures";
import { ipc, pending } from "./ipc";

const settings = { ...SETTINGS, career_os_sign_in: true, email_sign_in: true };
const googleEmail = "google.user@gmail.example";
const hubrubyEmail = "hubruby.other@example.com";
const screenProps = {
  running: false,
  section: "account" as const,
  update: { state: "none" } as const,
  check: { busy: false, error: null, start: () => {} },
};

describe("HubRuby sign-in intent", () => {
  it("signs out of Google and starts a separate HubRuby sign-in for another email", async () => {
    let current = settingsView({ settings, signed_in_as: googleEmail, signed_in_with: "browser" });
    ipc.api("GET", /.*/, () => pending());
    ipc.api("GET", /^\/me$/, () => ({
      sign_in_methods: [{ provider: current.signed_in_with === "career_os" ? "career_os" : "google", label: current.signed_in_as }],
    }));
    ipc.handle("sign_out", () => {
      current = { ...current, signed_in_as: null, signed_in_with: null, ready: false };
    });
    ipc.handle("career_os_sign_in", () => {
      current = { ...current, signed_in_as: hubrubyEmail, signed_in_with: "career_os", ready: true };
      return { status: "signed_in", name: hubrubyEmail, account: "existing" };
    });
    function AccountSettings() {
      const [view, setView] = useState(current);
      return <SettingsScreen {...screenProps} view={view} onSaved={() => setView(current)} />;
    }
    render(<ToastProvider><AccountSettings /></ToastProvider>);
    expect(screen.getByText(`Signed in as ${googleEmail}, with Google.`)).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await userEvent.click(await screen.findByRole("button", { name: "Continue with HubRuby" }));

    expect(await screen.findByText(`Signed in as ${hubrubyEmail}, with HubRuby.`)).toBeVisible();
    expect(ipc.calls.filter(call => call.cmd === "sign_out" || call.cmd === "career_os_sign_in")).toEqual([
      { cmd: "sign_out", args: {} },
      { cmd: "career_os_sign_in", args: { linkAccount: false } },
    ]);
    expect(screen.queryByText(/already linked to a different HubRuby account/)).not.toBeInTheDocument();
  });

  it("adds HubRuby to the current account only through the explicit Settings action", async () => {
    let added = false;
    ipc.api("GET", /.*/, () => pending());
    ipc.api("GET", /^\/me$/, () => ({
      sign_in_methods: [
        { provider: "google", label: googleEmail },
        ...(added ? [{ provider: "career_os", label: hubrubyEmail }] : []),
      ],
    }));
    ipc.handle("career_os_sign_in", () => {
      added = true;
      return { status: "added" };
    });
    const onSaved = vi.fn();
    render(
      <ToastProvider>
        <SettingsScreen {...screenProps} view={settingsView({ settings, signed_in_as: googleEmail })} onSaved={onSaved} />
      </ToastProvider>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Add HubRuby sign-in" }));

    expect(await screen.findByText("HubRuby sign-in added.")).toBeVisible();
    expect(ipc.calls.filter(call => call.cmd === "career_os_sign_in")).toEqual([
      { cmd: "career_os_sign_in", args: { linkAccount: true } },
    ]);
    expect(onSaved).toHaveBeenCalledOnce();
    expect(screen.getByText(`Signed in as ${googleEmail}, with Google.`)).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Add HubRuby sign-in" })).not.toBeInTheDocument());
  });
});
