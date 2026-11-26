import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { Banner, Button, Dialog, ToastProvider } from "../src/ui";
import { DataActivity } from "../src/usePolling";
import { NotificationVisibility } from "../src/ui/NotificationScope";

it("floats page notices, hides inactive pages, and lets a new error be dismissed independently", async () => {
  const tree = (active: boolean, text: string, visible = true) => <ToastProvider><main><DataActivity.Provider value={active}><NotificationVisibility.Provider value={visible}><Banner tone="danger">{text}</Banner></NotificationVisibility.Provider></DataActivity.Provider></main></ToastProvider>;
  const view = render(tree(true, "Connection lost"));
  expect(screen.getByRole("alert").closest(".ui-toast-viewport")).not.toBeNull();
  expect(screen.getByRole("main")).not.toContainElement(screen.getByRole("alert"));
  view.rerender(tree(false, "Connection lost"));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  view.rerender(tree(true, "Connection lost", false));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  view.rerender(tree(true, "Connection lost"));
  await userEvent.click(screen.getByRole("button", {name:"Dismiss notification"}));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  view.rerender(tree(true, "Authentication expired"));
  expect(screen.getByRole("alert")).toHaveTextContent("Authentication expired");
});

it("keeps floating notification actions inside a modal focus trap", async () => {
  const retry = vi.fn();
  render(<ToastProvider><Dialog open title="Save document" onOpenChange={() => {}}><Banner tone="danger" action={<Button onClick={retry}>Try again</Button>}>Saving failed</Banner><Button>Cancel edit</Button></Dialog></ToastProvider>);
  const dialog = screen.getByRole("dialog", {name:"Save document"});
  const notice = within(dialog).getByRole("alert");
  expect(notice.closest(".ui-toast-viewport")).not.toBeNull();
  expect(notice.closest(".ui-dialog-body")).toBeNull();
  for(let i=0;i<5 && document.activeElement!==screen.getByRole("button", {name:"Try again"});i++) await userEvent.tab();
  expect(screen.getByRole("button", {name:"Try again"})).toHaveFocus();
  await userEvent.keyboard("{Enter}"); expect(retry).toHaveBeenCalledOnce();
});