import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { expect, it } from "vitest";
import { TrayMenu } from "../src/TrayMenu";
import { ipc } from "./ipc";

it("opens with two concise actions and supports keyboard exit", async () => {
  ipc.handle("tray_menu_ready", () => false);
  render(<TrayMenu />);
  await waitFor(() => expect(screen.getByRole("menuitem", {name:"Pause suggestions"})).toHaveFocus());
  expect(screen.getAllByRole("menuitem")).toHaveLength(2);
  expect(screen.queryByText("Open CoRuby")).not.toBeInTheDocument();
  await userEvent.keyboard("{ArrowDown}{Enter}");
  expect(ipc.calls.find(c => c.cmd === "tray_menu_action")?.args).toEqual({action:"exit"});
});

it("refreshes paused state when reopened and Escape dismisses without quitting", async () => {
  ipc.handle("tray_menu_ready", () => false);
  render(<TrayMenu />);
  await waitFor(() => expect(screen.getByRole("menuitem", {name:"Pause suggestions"})).toHaveFocus());
  await act(() => emit("copilot://tray-menu", true));
  expect(screen.getByRole("menuitem", {name:"Resume suggestions"})).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(ipc.calls.filter(c => c.cmd === "tray_menu_action").map(c => c.args)).toEqual([{action:"dismiss"}]);
});