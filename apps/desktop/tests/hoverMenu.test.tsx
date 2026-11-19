import { act, renderHook } from "@testing-library/react";
import type { PointerEvent } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useHoverMenu } from "../src/ui/useHoverMenu";

const pointer = (pointerType = "mouse", buttons = 0) => ({ pointerType, buttons }) as PointerEvent;
const advance = (time: number) => act(() => { vi.advanceTimersByTime(time); });

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it("opens after a deliberate hover and allows crossing the trigger-to-panel gap", () => {
  const onOpen = vi.fn(), onClose = vi.fn();
  const { result } = renderHook(() => useHoverMenu({ onOpen, onClose }));
  act(() => result.current.onPointerEnter(pointer()));
  advance(119);
  expect(onOpen).not.toHaveBeenCalled();
  advance(1);
  expect(onOpen).toHaveBeenCalledOnce();

  act(() => result.current.onPointerLeave(pointer()));
  advance(180);
  act(() => result.current.onPointerEnter(pointer()));
  advance(240);
  expect(onClose).not.toHaveBeenCalled();

  act(() => result.current.onPointerLeave(pointer()));
  advance(239);
  expect(onClose).not.toHaveBeenCalled();
  advance(1);
  expect(onClose).toHaveBeenCalledOnce();
});

it("does not open during a passing hover, touch, or drag", () => {
  const onOpen = vi.fn(), onClose = vi.fn();
  const { result } = renderHook(() => useHoverMenu({ onOpen, onClose }));
  act(() => result.current.onPointerEnter(pointer()));
  advance(80);
  act(() => result.current.onPointerLeave(pointer()));
  advance(240);
  expect(onOpen).not.toHaveBeenCalled();
  onClose.mockClear();

  for (const event of [pointer("touch"), pointer("mouse", 1)]) {
    act(() => result.current.onPointerEnter(event));
    advance(500);
    act(() => result.current.onPointerLeave(event));
    advance(500);
  }
  expect(onOpen).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

it("uses current callbacks and preserves a menu whose keyboard focus is inside", () => {
  const firstOpen = vi.fn(), secondOpen = vi.fn(), onClose = vi.fn();
  const { result, rerender } = renderHook(({ open, focused }) => useHoverMenu({ onOpen: open, onClose, keepOpen: () => focused }), {
    initialProps: { open: firstOpen, focused: false },
  });
  act(() => result.current.onPointerEnter(pointer()));
  rerender({ open: secondOpen, focused: true });
  advance(120);
  expect(firstOpen).not.toHaveBeenCalled();
  expect(secondOpen).toHaveBeenCalledOnce();

  act(() => result.current.onPointerLeave(pointer()));
  advance(240);
  expect(onClose).not.toHaveBeenCalled();
  act(() => result.current.onPointerLeave(pointer()));
  rerender({ open: secondOpen, focused: false });
  advance(240);
  expect(onClose).toHaveBeenCalledOnce();
});

it("cancels pending transitions when explicitly dismissed or the window blurs", () => {
  const onOpen = vi.fn(), onClose = vi.fn();
  const { result } = renderHook(() => useHoverMenu({ onOpen, onClose, keepOpen: () => true }));
  act(() => result.current.onPointerEnter(pointer()));
  act(() => result.current.cancel());
  advance(500);
  expect(onOpen).not.toHaveBeenCalled();

  act(() => result.current.onPointerEnter(pointer()));
  act(() => { window.dispatchEvent(new Event("blur")); });
  advance(500);
  expect(onOpen).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledOnce();
});

it("clears open and close timers and the window listener when unmounted", () => {
  const onOpen = vi.fn(), onClose = vi.fn();
  const opening = renderHook(() => useHoverMenu({ onOpen, onClose }));
  act(() => opening.result.current.onPointerEnter(pointer()));
  opening.unmount();
  const closing = renderHook(() => useHoverMenu({ onOpen, onClose }));
  act(() => closing.result.current.onPointerLeave(pointer()));
  closing.unmount();
  act(() => { window.dispatchEvent(new Event("blur")); });
  advance(500);
  expect(onOpen).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});
