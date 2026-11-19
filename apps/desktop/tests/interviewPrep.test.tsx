import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApplicationDetail } from "../src/api";
import { Applications } from "../src/Applications";
import { ToastProvider } from "../src/ui";
import { app, detail, interview, resume } from "./fixtures";
import { ipc } from "./ipc";

function routes() {
  let application: ApplicationDetail = {
    ...detail(app("a1", "Northwind", "Backend engineer")), note: "Discuss the routing project.",
    resume: { version_id: "rt", kind: "tailored", label: "Northwind backend.pdf", status: "extracted", manual: false },
    versions: [{ id: "rb", kind: "base", label: "Alex general.pdf", status: "extracted" }, { id: "rt", kind: "tailored", label: "Northwind backend.pdf", status: "extracted" }],
  };
  ipc.api("GET", /^\/applications$/, () => ({ applications: [application] }));
  ipc.api("GET", /^\/applications\/a1$/, () => application);
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [interview("i1", "a1", "Northwind", 60)] }));
  ipc.api("GET", /^\/career-os\/connection$/, () => ({ connected: false, available: false }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: resume("rb", "base", "Alex general.pdf"), versions: [] }));
  ipc.api("PATCH", /^\/applications\/a1$/, (_path, body) => {
    const values = body as Partial<ApplicationDetail> & { resume_version_id?: string };
    application = { ...application, ...values };
    if (values.resume_version_id) {
      const chosen = application.versions.find(v => v.id === values.resume_version_id)!;
      application = { ...application, resume: { version_id: chosen.id, kind: chosen.kind, label: chosen.label, status: chosen.status, manual: true } };
    }
    return application;
  });
  return { get: () => application, set: (next: ApplicationDetail) => { application = next; } };
}
const wrap = (props: React.ComponentProps<typeof Applications> = {}) => render(<ToastProvider><Applications {...props} /></ToastProvider>);

describe("Interview prep workspace", () => {
  it("keeps the application, resume and job description together without nested tabs", async () => {
    routes(); const start = vi.fn(); wrap({ onStartInterview: start });
    expect(await screen.findByRole("heading", { name: "Northwind" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Interview prep" })).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    const application = within(screen.getByRole("region", { name: "Application" }));
    expect(application.getByRole("heading", { name: "Resume" })).toBeInTheDocument();
    expect(application.getByText("Northwind backend.pdf")).toBeInTheDocument();
    expect(application.getByRole("heading", { name: "Job description" })).toBeInTheDocument();
    expect(application.getByText("We build routing.")).toBeInTheDocument();
    expect(application.queryByRole("textbox")).not.toBeInTheDocument();
    await userEvent.click(application.getByRole("button", { name: /Start a call for Northwind/ }));
    expect(start).toHaveBeenCalledWith("i1");
    expect(within(screen.getByRole("list", { name: "All applications" })).getByText("Northwind backend.pdf")).toBeInTheDocument();
  });
  it("changes the paired resume inside the application and refreshes its summary", async () => {
    routes(); wrap();
    await userEvent.click(await screen.findByRole("button", { name: "Change resume" }));
    await userEvent.click(screen.getByRole("combobox", { name: "Resume for this application" }));
    await userEvent.click(screen.getByRole("option", { name: "Default: Alex general.pdf" }));
    await waitFor(() => expect(ipc.apiCalls("PATCH")[0].body).toEqual({ resume_version_id: "rb" }));
    expect(within(screen.getByRole("region", { name: "Resume" })).getByText("Alex general.pdf")).toBeInTheDocument();
  });
  it("edits the job description inline and returns to the saved document", async () => {
    routes(); wrap();
    await userEvent.click(await screen.findByRole("button", { name: "Edit job description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Job description" }), { target: { value: "Build reliable routing tools." } });
    await userEvent.click(screen.getByRole("button", { name: "Save description" }));
    expect(await screen.findByText("Build reliable routing tools.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Job description" })).not.toBeInTheDocument();
    expect(ipc.apiCalls("PATCH")[0].body).toEqual({ jd_text: "Build reliable routing tools." });
  });
  it("saves notes without overwriting identity or resume pairing", async () => {
    routes(); wrap(); await userEvent.click(await screen.findByRole("button", { name: "Edit notes" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Call notes" }), { target: { value: "Mention cross-team work." } });
    await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
    expect(await screen.findByText("Mention cross-team work.")).toBeInTheDocument();
    expect(ipc.apiCalls("PATCH")[0].body).toEqual({ note: "Mention cross-team work." });
  });
  it("adds and deletes an application-owned tailored resume from its change editor", async () => {
    const state = routes();
    ipc.api("POST", /^\/resumes$/, (_path, body) => {
      const values = body as { label: string };
      state.set({ ...state.get(), versions: [...state.get().versions, { id: "rt2", kind: "tailored", label: values.label, status: "extracted" }] });
      return resume("rt2", "tailored", values.label);
    });
    ipc.api("DELETE", /^\/resumes\/rt2$/, () => { state.set({ ...state.get(), versions: state.get().versions.filter(v => v.id !== "rt2") }); return null; });
    wrap(); await userEvent.click(await screen.findByRole("button", { name: "Change resume" }));
    await userEvent.click(screen.getByRole("button", { name: "Add tailored resume" }));
    await userEvent.type(screen.getByLabelText(/Resume name/), "Northwind v3");
    await userEvent.type(screen.getByLabelText("Resume text"), "Experience building routing products.");
    await userEvent.click(screen.getByRole("button", { name: "Add pasted resume" }));
    expect(await screen.findByRole("button", { name: "Actions for Northwind v3" })).toBeInTheDocument();
    expect(ipc.apiCalls("POST")[0].body).toEqual({ kind: "tailored", application_id: "a1", label: "Northwind v3", text: "Experience building routing products." });
    await userEvent.click(screen.getByRole("button", { name: "Actions for Northwind v3" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete…" }));
    expect(ipc.apiCalls("DELETE")).toHaveLength(0);
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete resume" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Actions for Northwind v3" })).not.toBeInTheDocument());
    expect(ipc.apiCalls("DELETE")[0].path).toBe("/resumes/rt2");
  });
  it("opens the shared default editor on an external request and saves there", async () => {
    routes(); ipc.api("POST", /^\/resumes$/, () => resume("rb", "base", "New base"));
    const { rerender } = wrap(); await screen.findByText("Alex general.pdf");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    rerender(<ToastProvider><Applications baseResumeRequest={1} /></ToastProvider>);
    const dialog = await screen.findByRole("dialog", { name: "Replace the base resume" });
    await userEvent.type(within(dialog).getByLabelText("Resume text"), "My professional experience.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save pasted text" }));
    await waitFor(() => expect(ipc.apiCalls("POST")[0].body).toEqual({ kind: "base", label: null, text: "My professional experience." }));
  });
});
