import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { LiveCallLanguage } from "../src/LiveCallLanguage";
import { initial, type SessionView } from "../src/session";
import { ipc } from "./ipc";

afterEach(() => vi.useRealTimers());
const live = (): SessionView => ({ ...initial, running: true, sessionId: "session-one", connection: { state: "live", session_id: "session-one", resumed: false }, spokenLanguage: "en", languageSwitching: true });
function catalog() { ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }, { id: "vi", name: "Vietnamese", native_name: "Tiếng Việt" }, { id: "ar", name: "Arabic", native_name: "العربية", direction: "rtl" }] })); }
async function pick() {
  const button = screen.getByRole("button", { name: "Call language" });
  await waitFor(() => expect(button).toBeEnabled());
  await userEvent.click(button);
  await userEvent.type(screen.getByRole("combobox", { name: "Search call languages" }), "viet");
  await userEvent.keyboard("{Enter}");
  return button;
}
const sent = () => ipc.calls.filter(call => call.cmd === "set_spoken_language");

it("keeps the confirmed selection until a matching acknowledgement, then shows the new language", async () => {
  catalog(); const session = live(); const view = render(<LiveCallLanguage session={session} />);
  const button = await pick();
  expect(sent()).toHaveLength(1);
  const requestId = sent()[0].args.requestId as string;
  expect(sent()[0].args).toMatchObject({ language: "vi", requestId: expect.any(String) });
  expect(button).toHaveTextContent("English"); expect(button).toBeDisabled();
  expect(button).toHaveAccessibleDescription("English");
  expect(screen.getByRole("status")).toHaveTextContent("Changing to Vietnamese");
  view.rerender(<LiveCallLanguage session={{ ...session, languageResult: { requestId: "another-request", error: null, seq: 2 } }} />);
  expect(button).toBeDisabled();
  view.rerender(<LiveCallLanguage session={{ ...session, spokenLanguage: "vi", languageResult: { requestId, error: null, seq: 3 } }} />);
  await waitFor(() => expect(button).toBeEnabled());
  expect(button).toHaveTextContent("Tiếng Việt · Vietnamese");
  expect(ipc.apiCalls("PATCH")).toHaveLength(0);
});

it("shows a correlated rejection without claiming the requested language is active", async () => {
  catalog(); const session = live(); const view = render(<LiveCallLanguage session={session} compact />);
  const button = await pick();
  view.rerender(<LiveCallLanguage session={{ ...session, languageResult: { requestId: sent()[0].args.requestId as string, error: "The recognition provider is unavailable. Try again.", seq: 2 } }} compact />);
  expect(await screen.findByRole("alert")).toHaveTextContent("recognition provider is unavailable");
  expect(button).toHaveTextContent("English"); expect(button).toBeEnabled();
});

it("handles native command failure and allows retry", async () => {
  catalog(); ipc.handle("set_spoken_language", () => { throw new Error("Socket disconnected"); });
  render(<LiveCallLanguage session={live()} />);
  const button = await pick();
  expect(await screen.findByRole("alert")).toHaveTextContent("Socket disconnected");
  expect(button).toBeEnabled(); expect(button).toHaveTextContent("English");
});

it("clears pending after disconnect and accepts the authoritative resumed language", async () => {
  catalog(); const session = live(); const view = render(<LiveCallLanguage session={session} />);
  const button = await pick();
  view.rerender(<LiveCallLanguage session={{ ...session, connection: { state: "reconnecting", attempt: 1, in_ms: 1000, close_code: null } }} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection interrupted");
  expect(button).toBeDisabled();
  view.rerender(<LiveCallLanguage session={{ ...session, spokenLanguage: "ar" }} />);
  await waitFor(() => expect(button).toBeEnabled());
  expect(button).toHaveTextContent("العربية · Arabic");
  expect(button.querySelector("bdi")).toHaveAttribute("dir", "rtl");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("shows catalog errors and retries without sending a language command", async () => {
  ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: null }] }));
  render(<LiveCallLanguage session={live()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("invalid language settings");
  expect(screen.getByRole("button", { name: "Call language" })).toBeDisabled();
  catalog(); await userEvent.click(screen.getByRole("button", { name: "Retry languages" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Call language" })).toBeEnabled());
  expect(sent()).toHaveLength(0);
});

it("does not enable changing against old gateways or while reconnecting", async () => {
  catalog(); const session = live(); const view = render(<LiveCallLanguage session={{ ...session, languageSwitching: false }} />);
  expect(await screen.findByText("Update the server to change language during calls.")).toBeVisible();
  await waitFor(() => expect(ipc.apiCalls("GET")).toHaveLength(1));
  expect(screen.getByRole("button", { name: "Call language" })).toBeDisabled();
  view.rerender(<LiveCallLanguage session={{ ...session, connection: { state: "connecting", attempt: 1 } }} />);
  expect(screen.getByRole("button", { name: "Call language" })).toBeDisabled();
  expect(sent()).toHaveLength(0);
});

it("times out honestly and still accepts a late correlated acknowledgement", async () => {
  catalog(); const session = live(); const view = render(<LiveCallLanguage session={session} />);
  const button = screen.getByRole("button", { name: "Call language" });
  await waitFor(() => expect(button).toBeEnabled());
  vi.useFakeTimers();
  fireEvent.click(button); fireEvent.click(screen.getByRole("option", { name: "Tiếng Việt · Vietnamese" }));
  await act(async () => vi.advanceTimersByTime(30000));
  expect(screen.getByRole("alert")).toHaveTextContent("not confirmed"); expect(button).toHaveTextContent("English");
  view.rerender(<LiveCallLanguage session={{ ...session, spokenLanguage: "vi", languageResult: { requestId: sent()[0].args.requestId as string, error: null, seq: 8 } }} />);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument(); expect(button).toHaveTextContent("Vietnamese");
});
