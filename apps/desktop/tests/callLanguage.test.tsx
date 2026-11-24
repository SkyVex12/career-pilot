import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { CallLanguage } from "../src/CallLanguage";
import { ipc } from "./ipc";
import corpus from "../../../tests/fixtures/call_languages.json";

describe("profile call language", () => {
  it("opens Customize by default and respects collapsing it through preference updates", async () => {
    let finish!: (value: { spoken_language: string }) => void;
    ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }] }));
    ipc.api("GET", /^\/profile\/call-preferences$/, () => new Promise(resolve => { finish = resolve; }));
    const change = vi.fn();
    const view = render(<CallLanguage value="en" onChange={change} />);
    const disclosure = screen.getByText("Customize").closest("details")!;
    expect(disclosure).toHaveAttribute("open");
    await userEvent.click(screen.getByText("Customize"));
    expect(disclosure).not.toHaveAttribute("open");
    finish({ spoken_language: "en" });
    await waitFor(() => expect(change).toHaveBeenCalledWith("en"));
    view.rerender(<CallLanguage value="en" onChange={change} disabled />);
    expect(disclosure).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("Customize"));
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Language" })).toBeVisible();
  });

  it("loads the profile default and changes the call language without rewriting it", async () => {
    ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }, { id: "es", name: "Spanish" }] }));
    ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "es" }));
    ipc.api("PATCH", /^\/profile\/call-preferences$/, (_p, body) => body);
    const change = vi.fn(); const ready = vi.fn();
    const { container } = render(<CallLanguage value="es" onChange={change} onReady={ready} />);
    await waitFor(() => expect(change).toHaveBeenCalledWith("es"));
    expect(ready).toHaveBeenLastCalledWith(true);
    expect(container.querySelector("select")!.options).toHaveLength(2);
    expect(screen.queryByLabelText("Use as default")).not.toBeInTheDocument();
    fireEvent.change(container.querySelector("select")!, { target: { value: "en" } });
    expect(change).toHaveBeenLastCalledWith("en");
    expect(ipc.apiCalls("PATCH")).toHaveLength(0);
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

it("shows error details on hover and retries when Customize is reopened", async () => {
  ipc.api("GET", /^\/call-languages$/, () => { throw "Could not reach the language server"; });
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "en" }));
  const ready = vi.fn();
  render(<CallLanguage value="en" onChange={() => {}} onReady={ready} />);
  await screen.findByRole("alert");
  const disclosure = screen.getByText("Customize").closest("details")!;
  const axe = (await import("axe-core")).default;
  const accessibility = await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
  expect(accessibility.violations.map(violation => violation.id)).toEqual([]);
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  await userEvent.hover(screen.getByRole("button", { name: "About Language error" }));
  expect(await screen.findByRole("tooltip")).toHaveTextContent("Could not reach the language server");
  expect(ready).not.toHaveBeenCalledWith(true);
  ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }] }));
  expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByText("Customize"));
  expect(disclosure).not.toHaveAttribute("open");
  await userEvent.click(screen.getByText("Customize"));
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  expect(disclosure).toHaveAttribute("open");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("renders the configured languages with native labels and isolated right-to-left text", async () => {
  const items = corpus.map(item => ({ id: item.language, name: item.language === "ar" ? "Arabic" : item.language,
    native_name: item.language === "ar" ? "العربية" : item.language, direction: ["ar", "fa", "ur"].includes(item.language) ? "rtl" : "ltr" }));
  ipc.api("GET", /^\/call-languages$/, () => ({ items }));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "ar" }));
  const { container } = render(<CallLanguage value="ar" onChange={() => {}} />);
  await waitFor(() => expect(container.querySelector("select")!.options).toHaveLength(corpus.length));
  const picker = screen.getByRole("button", { name: "Language" });
  expect(picker).toHaveTextContent("العربية · Arabic");
  expect(picker.querySelector("bdi")).toHaveAttribute("dir", "rtl");
  expect(picker.querySelector("bdi")).toHaveAttribute("lang", "ar");
  fireEvent.click(picker);
  const option = screen.getByRole("option", { name: "العربية · Arabic" });
  expect(option.querySelector("bdi")).toHaveAttribute("dir", "rtl");
  expect(option.querySelector("bdi")).toHaveAttribute("lang", "ar");
});

it("searches a large catalog by English, native name and code, and changes only after selection", async () => {
  const items = [
    { id: "en", name: "English" },
    { id: "vi", name: "Vietnamese", native_name: "Tiếng Việt" },
    { id: "ar", name: "Arabic", native_name: "العربية", direction: "rtl" },
    ...Array.from({ length: 110 }, (_, index) => ({ id: `q${String.fromCharCode(97 + Math.floor(index / 26))}${String.fromCharCode(97 + index % 26)}`, name: `Fixture language ${index}` })),
  ];
  ipc.api("GET", /^\/call-languages$/, () => ({ items }));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "en" }));
  ipc.api("PATCH", /^\/profile\/call-preferences$/, (_p, body) => body);
  function Picker() {
    const [value, setValue] = useState("en");
    return <CallLanguage value={value} onChange={setValue} />;
  }
  render(<Picker />);
  const picker = screen.getByRole("button", { name: "Language" });
  await waitFor(() => expect(picker).toBeEnabled());
  await userEvent.click(picker);
  const search = screen.getByRole("combobox", { name: "Search languages" });
  expect(search).toHaveFocus();
  expect(screen.getAllByRole("option")).toHaveLength(113);
  fireEvent.keyDown(search, { key: "Enter", isComposing: true });
  expect(search).toHaveFocus();
  expect(ipc.apiCalls("PATCH")).toHaveLength(0);
  await userEvent.type(search, "vietnam");
  expect(screen.getAllByRole("option")).toHaveLength(1);
  expect(screen.getByRole("option")).toHaveTextContent("Tiếng Việt · Vietnamese");
  await userEvent.clear(search);
  await userEvent.type(search, "tieng viet");
  expect(screen.getByRole("option")).toHaveTextContent("Tiếng Việt · Vietnamese");
  await userEvent.clear(search);
  await userEvent.type(search, "العربية");
  expect(screen.getByRole("option")).toHaveTextContent("العربية · Arabic");
  await userEvent.clear(search);
  await userEvent.type(search, "qef");
  expect(screen.getAllByRole("option")).toHaveLength(1);
  expect(ipc.apiCalls("PATCH")).toHaveLength(0);
  expect(picker).toHaveTextContent("English");
  await userEvent.keyboard("{Enter}");
  expect(ipc.apiCalls("PATCH")).toHaveLength(0);
  expect(picker).toHaveFocus();
  expect(picker).toHaveTextContent("Fixture language 109");
  expect(screen.queryByRole("dialog", { name: "Language" })).not.toBeInTheDocument();
});

it("leaves the selection unchanged for an empty search and cancels cleanly with Escape", async () => {
  ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }, { id: "es", name: "Español · Spanish" }] }));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "en" }));
  const change = vi.fn();
  render(<CallLanguage value="en" onChange={change} />);
  const picker = screen.getByRole("button", { name: "Language" });
  await waitFor(() => expect(picker).toBeEnabled());
  await userEvent.click(picker);
  await userEvent.type(screen.getByRole("combobox", { name: "Search languages" }), "not a language");
  expect(screen.getByRole("status")).toHaveTextContent("No matches");
  expect(screen.queryByRole("option")).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Search languages" })).not.toHaveAttribute("aria-activedescendant");
  await userEvent.keyboard("{Enter}{Escape}");
  expect(change).toHaveBeenCalledTimes(1);
  expect(picker).toHaveFocus();
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("combobox", { name: "Search languages" })).toHaveValue("");
  await userEvent.keyboard("{ArrowDown}{Enter}");
  expect(change).toHaveBeenLastCalledWith("es");
});

it("closes the searchable popup and continues to the next field with Tab", async () => {
  ipc.api("GET", /^\/call-languages$/, () => ({ items: [{ id: "en", name: "English" }] }));
  ipc.api("GET", /^\/profile\/call-preferences$/, () => ({ spoken_language: "en" }));
  render(<CallLanguage value="en" onChange={() => {}} answerFormat={<button>Next option</button>} />);
  const picker = screen.getByRole("button", { name: "Language" });
  await waitFor(() => expect(picker).toBeEnabled());
  await userEvent.click(picker);
  await userEvent.tab();
  expect(screen.queryByRole("dialog", { name: "Language" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Next option" })).toHaveFocus();
});

it("explains legacy English compatibility without blocking older-server calls or saving unsupported settings", async () => {
  const change = vi.fn(), ready = vi.fn();
  render(<CallLanguage value="en" onChange={change} onReady={ready} />);
  expect(await screen.findByText(/Using English compatibility mode/)).toHaveTextContent("Update the CoRuby server");
  expect(change).toHaveBeenLastCalledWith("en");
  expect(ready).toHaveBeenLastCalledWith(true);
  expect(screen.getByRole("button", { name: "Language" })).toBeEnabled();
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
