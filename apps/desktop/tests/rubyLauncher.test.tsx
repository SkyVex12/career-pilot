import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { RubyLauncher } from "../src/RubyLauncher";

afterEach(() => vi.unstubAllGlobals());
it("moves the native window after a drag threshold, without opening the card", () => {
  vi.stubGlobal("PointerEvent", MouseEvent);
  const open = vi.fn(), drag = vi.fn();
  render(<RubyLauncher onOpen={open} onDrag={drag} />);
  const ruby = screen.getByRole("button", { name: "Expand compact toolbar" });
  fireEvent.pointerDown(ruby, { button: 0, clientX: 10, clientY: 10 });
  fireEvent.pointerMove(ruby, { clientX: 12, clientY: 11 });
  expect(drag).not.toHaveBeenCalled();
  fireEvent.pointerMove(ruby, { clientX: 20, clientY: 15 });
  fireEvent.pointerMove(ruby, { clientX: 25, clientY: 15 });
  fireEvent.pointerUp(ruby);
  fireEvent.click(ruby, { detail: 1 });
  expect(drag).toHaveBeenCalledTimes(1);
  expect(open).not.toHaveBeenCalled();
  fireEvent.pointerDown(ruby, { button: 0, clientX: 10, clientY: 10 });
  fireEvent.pointerUp(ruby);
  fireEvent.click(ruby, { detail: 1 });
  expect(open).toHaveBeenCalledTimes(1);
});
it("opens from keyboard activation", () => {
  const open = vi.fn();
  render(<RubyLauncher onOpen={open} onDrag={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Expand compact toolbar" }), { detail: 0 });
  expect(open).toHaveBeenCalledOnce();
});
