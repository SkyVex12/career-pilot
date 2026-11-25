import { useEffect, useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";

/** A nonmodal panel anchored to its button. Outside clicks still reach their target;
 * Escape closes a nested select before the panel and returns focus to the opener. */
export function useOverlayPopover(
  open: boolean,
  close: () => void,
  trigger: RefObject<HTMLElement | null>,
  panel: RefObject<HTMLElement | null>,
  align: "start" | "end" = "end",
): CSSProperties {
  const [position, setPosition] = useState<CSSProperties>({ position: "fixed", visibility: "hidden" });
  useLayoutEffect(() => {
    if (!open) return;
    const fit = () => {
      const button = trigger.current, content = panel.current;
      if (!button || !content) return;
      const anchor = button.getBoundingClientRect(), edge = 8, gap = 6;
      const width = Math.min(content.getBoundingClientRect().width || 312, window.innerWidth - edge * 2);
      const below = window.innerHeight - anchor.bottom - gap - edge;
      const above = anchor.top - gap - edge;
      const up = below < Math.min(content.scrollHeight, 280) && above > below;
      const maxHeight = Math.max(80, up ? above : below);
      const height = Math.min(content.scrollHeight, maxHeight);
      setPosition({ position: "fixed", visibility: "visible", width, maxWidth: "calc(100vw - 16px)", maxHeight,
        left: Math.max(edge, Math.min(align === "start" ? anchor.left : anchor.right - width, window.innerWidth - width - edge)),
        top: up ? Math.max(edge, anchor.top - gap - height) : anchor.bottom + gap,
      });
    };
    fit();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    if (panel.current) observer?.observe(panel.current);
    if (trigger.current) observer?.observe(trigger.current);
    window.addEventListener("resize", fit);
    return () => { observer?.disconnect(); window.removeEventListener("resize", fit); };
  }, [open, trigger, panel, align]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Element;
      if (panel.current?.contains(target) || trigger.current?.contains(target) || target.closest?.(".ui-select-menu, .ui-toast-viewport")) return;
      close();
    };
    const escape = (event: KeyboardEvent) => {
      if ((event.target as Element | null)?.closest?.(".ui-dialog")) return;
      if (event.key !== "Escape" || event.defaultPrevented || document.querySelector('[role="combobox"][aria-expanded="true"]')) return;
      event.preventDefault();
      close();
      trigger.current?.focus({ preventScroll: true });
    };
    const scroll = (event: Event) => {
      const target = event.target;
      if (target instanceof Element && (panel.current?.contains(target) || target.closest(".ui-select-menu"))) return;
      close();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    document.addEventListener("scroll", scroll, true);
    window.addEventListener("blur", close);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
      document.removeEventListener("scroll", scroll, true);
      window.removeEventListener("blur", close);
    };
  }, [open, close, trigger, panel]);
  return position;
}
