import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CallLanguage } from "../src/CallLanguage";
import { ipc } from "./ipc";
import corpus from "../../../tests/fixtures/call_languages.json";

describe("profile call language", () => {
  it("loads the profile default, offers only enabled languages and saves explicitly", async () => {
    ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }, { id: "es", name: "Spanish" }] }));
    ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "es" }));
    ipc.api("PATCH", /^\/profile\/call-preferences$/, (_p, body) => body);
    const change = vi.fn(); const ready = vi.fn();
    const { container } = render(<CallLanguage value="es" onChange={change} onReady={ready} />);
    await waitFor(() => expect(change).toHaveBeenCalledWith("es"));
    expect(ready).toHaveBeenLastCalledWith(true);
    expect(container.querySelector("select")!.options).toHaveLength(2);
    fireEvent.click(screen.getByLabelText("Save selection as this profile’s default"));
    await waitFor(() => expect(ipc.apiCalls("PATCH")).toHaveLength(1));
    fireEvent.change(container.querySelector("select")!, { target: { value: "en" } });
    await waitFor(() => expect(ipc.apiCalls("PATCH")[1].body).toEqual({ spoken_language: "en" }));
  });
  it("does not silently replace an unavailable persisted language", async () => {
    ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }] }));
    ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "vi" }));
    const change = vi.fn(); const ready = vi.fn();
    render(<CallLanguage value="vi" onChange={change} onReady={ready} />);
    await waitFor(() => expect(change).toHaveBeenCalledWith("vi"));
    expect(ready).toHaveBeenLastCalledWith(false);
    expect(ipc.apiCalls("PATCH")).toHaveLength(0);
  });
  it("ignores a prior profile's delayed preference after unmount", async () => {
    let resolve!: (value: { spoken_language: string }) => void;
    ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }] }));
    ipc.api("GET", /^\/profile\/call-preferences$/, () => new Promise(r => { resolve = r; }));
    const change = vi.fn(); const view = render(<CallLanguage value="en" onChange={change} />);
    await waitFor(() => expect(resolve).toBeDefined()); view.unmount(); resolve({ spoken_language: "es" });
    await Promise.resolve(); expect(change).not.toHaveBeenCalled();
  });
});

it("keeps call start unavailable when a server returns malformed language settings", async () => {
  ipc.api("GET", /^\/call-languages$/, () => ({}));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "en" }));
  const ready = vi.fn(); render(<CallLanguage value="en" onChange={() => {}} onReady={ready} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("invalid language settings");
  expect(ready).not.toHaveBeenCalledWith(true);
});

it("renders all 24 configured languages with native labels and isolated right-to-left text", async () => {
  const items = corpus.map(item => ({ id: item.language, name: item.language === "ar" ? "Arabic" : item.language,
    native_name: item.language === "ar" ? "العربية" : item.language, direction: ["ar", "fa", "ur"].includes(item.language) ? "rtl" : "ltr" }));
  ipc.api("GET", /^\/call-languages$/, () => ({ items }));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "ar" }));
  const { container } = render(<CallLanguage value="ar" onChange={() => {}} />);
  await waitFor(() => expect(container.querySelector("select")!.options).toHaveLength(24));
  fireEvent.click(screen.getByText("Customize"));
  const picker = screen.getByRole("combobox", { name: "Language" });
  expect(picker).toHaveTextContent("العربية · Arabic");
  expect(picker.querySelector("bdi")).toHaveAttribute("dir", "rtl");
  expect(picker.querySelector("bdi")).toHaveAttribute("lang", "ar");
  fireEvent.click(picker);
  const option = screen.getByRole("option", { name: "العربية · Arabic" });
  expect(option.querySelector("bdi")).toHaveAttribute("dir", "rtl");
  expect(option.querySelector("bdi")).toHaveAttribute("lang", "ar");
});

it("explains legacy English compatibility without blocking older-server calls or saving unsupported settings", async () => {
  const change = vi.fn(), ready = vi.fn();
  render(<CallLanguage value="en" onChange={change} onReady={ready} />);
  expect(await screen.findByText(/Using English compatibility mode/)).toHaveTextContent("Update the Career Pilot server");
  expect(change).toHaveBeenLastCalledWith("en");
  expect(ready).toHaveBeenLastCalledWith(true);
  expect(screen.getByLabelText("Save selection as this profile’s default")).toBeDisabled();
  expect(ipc.apiCalls("PATCH")).toHaveLength(0);
});

it.each([
  [null], [{ id: "en", name: {} }], [{ id: "en", name: "English", direction: "vertical" }],
  [{ id: "ar", name: "Arabic", native_name: {} }], [{ id: "en", name: "English" }, { id: "en", name: "Duplicate" }],
])("rejects malformed language entries before publishing them: %j", async (...items) => {
  ipc.api("GET", /^\/call-languages$/, () => ({ items }));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "en" }));
  const ready = vi.fn(); render(<CallLanguage value="en" onChange={() => {}} onReady={ready} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("invalid language settings");
  expect(ready).not.toHaveBeenCalledWith(true);
});
