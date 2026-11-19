import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CallLanguage } from "../src/CallLanguage";
import { ipc } from "./ipc";

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
