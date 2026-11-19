import { useRef } from "react";
import { RubyMark } from "./ui/RubyMark";

/** Keep click actions separate from dragging in every window mode. */
export function RubyLauncher({ onOpen, onDrag, compact = false, disabled = false, className, label, title }: { onOpen: () => void; onDrag: () => void; compact?: boolean; disabled?: boolean; className?: string; label?: string; title?: string }) {
  const press = useRef<{ x: number; y: number } | null>(null);
  const dragged = useRef(false);
  return <button className={className ?? (compact ? "session-dot-control" : "session-dot")} disabled={disabled} aria-label={label ?? (compact ? "Expand full window" : "Expand compact toolbar")} title={title ?? (compact ? "Click to expand · Drag to move" : "Hover to open · Drag to move")}
    onPointerDown={e => {
      if (e.button !== 0) return;
      press.current = { x: e.clientX, y: e.clientY };
      dragged.current = false;
      e.currentTarget.setPointerCapture(e.pointerId);
    }}
    onPointerMove={e => {
      if (!press.current || dragged.current) return;
      if (Math.hypot(e.clientX - press.current.x, e.clientY - press.current.y) < 5) return;
      dragged.current = true;
      press.current = null;
      if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      onDrag();
    }}
    onPointerUp={() => { press.current = null; }}
    onPointerCancel={() => { press.current = null; }}
    onLostPointerCapture={() => { press.current = null; }}
    onClick={e => {
      if (dragged.current && e.detail !== 0) { e.preventDefault(); return; }
      onOpen();
    }}><RubyMark /></button>;
}
