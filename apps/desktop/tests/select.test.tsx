import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, it, vi } from "vitest";
import { Dialog, Field, Select } from "../src/ui";

function Example() {
  const [value, setValue] = useState("interview");
  return <><Field label="Session type" hint="Choose a mode"><Select value={value} onChange={e => setValue(e.target.value)}>
    <option value="interview">Interview</option><option value="unavailable" disabled>Unavailable</option>
    <optgroup label="Work"><option value="standup">Standup</option><option value="meeting">Meeting</option></optgroup>
  </Select></Field><button>Next</button></>;
}
it("selects by keyboard, skips disabled options, and preserves focus", async () => {
  render(<Example />);
  const combo = screen.getByRole("combobox", { name: "Session type" });
  expect(combo).toHaveAccessibleDescription("Choose a mode");
  combo.focus();
  await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
  expect(combo).toHaveTextContent("Standup");
  expect(combo).toHaveFocus();
  expect(screen.queryByRole("listbox")).toBeNull();
  await userEvent.keyboard("{ArrowDown}{Home}{Escape}");
  expect(combo).toHaveTextContent("Standup");
  await userEvent.keyboard("{ArrowDown}{Tab}");
  expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
});
it("supports typeahead and pointer selection without an OS picker", async () => {
  render(<Example />);
  const combo = screen.getByRole("combobox", { name: "Session type" });
  combo.focus();
  await userEvent.keyboard("m");
  expect(combo).toHaveTextContent("Meeting");
  await userEvent.click(combo);
  await userEvent.click(screen.getByRole("option", { name: "Interview" }));
  expect(combo).toHaveTextContent("Interview");
  await userEvent.click(combo);
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(screen.queryByRole("listbox")).toBeNull();
});
it("does not open a disabled control or select a disabled group", async () => {
  const change = vi.fn();
  render(<Select aria-label="Device" disabled onChange={change}><option>Default</option></Select>);
  await userEvent.click(screen.getByRole("combobox", { name: "Device" }));
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(change).not.toHaveBeenCalled();
});

it("keeps a dialog open while choosing an option or dismissing only its list", async () => {
  const onOpenChange = vi.fn();
  render(<Dialog open onOpenChange={onOpenChange} title="Edit"><Example /></Dialog>);
  const combo = screen.getByRole("combobox", { name: "Session type" });
  await userEvent.click(combo);
  expect(screen.getByRole("dialog")).toContainElement(screen.getByRole("listbox"));
  await userEvent.click(screen.getByRole("option", { name: "Standup" }));
  expect(combo).toHaveTextContent("Standup");
  expect(onOpenChange).not.toHaveBeenCalled();
  await userEvent.click(combo);
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(onOpenChange).not.toHaveBeenCalled();
});
