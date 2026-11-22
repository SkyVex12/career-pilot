import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { CompanionDevices } from "../src/CompanionDevices";
import { ipc } from "./ipc";
it("does not replace current display status with a previous call's delayed response", async () => {
 let finishOld!: (value: unknown) => void;
 ipc.api("GET", /^\/companion\/attachments$/, () => new Promise(resolve => { finishOld = resolve; }));
 const view = render(<CompanionDevices sessionId="old" />);
 const details = view.container.querySelector("details")!; details.open = true; fireEvent(details, new Event("toggle"));
 await screen.findByText("Loading authorized displays…");
 ipc.api("GET", /^\/companion\/attachments$/, () => ({ items: [{ attachment_id: "new-phone", session_id: "new", device_name: "Current phone", expires_at: "2099-01-01T00:00:00Z", revoked: false }] }));
 view.rerender(<CompanionDevices sessionId="new" />);
 expect(await screen.findByText("Current phone")).toBeVisible();
 await act(async () => { finishOld({ items: [{ attachment_id: "old-phone", session_id: "old", device_name: "Previous phone", expires_at: "2099-01-01T00:00:00Z", revoked: false }] }); });
 expect(screen.getByText("Current phone")).toBeVisible();
 expect(screen.queryByText("Previous phone")).not.toBeInTheDocument();
});
it("only lists active displays for the current call and preserves capture when hiding or disconnecting", async () => {
 const items=[{attachment_id:"a1",session_id:"s1",device_name:"My phone",expires_at:"2099-01-01T00:00:00Z",revoked:false},{attachment_id:"a2",session_id:"s2",device_name:"Other call",expires_at:"2099-01-01T00:00:00Z",revoked:false}];
 ipc.api("GET", /^\/companion\/attachments$/, () => ({items}));ipc.api("DELETE", /^\/companion\/attachments\/a1$/, () => {items[0].revoked=true;return {};});
 const {container}=render(<CompanionDevices sessionId="s1"/>);const details=container.querySelector("details")!;details.open=true;fireEvent(details,new Event("toggle"));
 await screen.findByText(/My phone/);expect(screen.queryByText(/Other call/)).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole("button",{name:"Hide desktop overlay"}));expect(ipc.calls.some(c=>c.cmd==="show_overlay"&&c.args.show===false)).toBe(true);
 fireEvent.click(screen.getByRole("button",{name:"Disconnect display"}));await waitFor(()=>expect(screen.queryByText(/My phone/)).not.toBeInTheDocument());
 expect(ipc.calls.some(c=>["start_session","end_session"].includes(c.cmd))).toBe(false);
});
