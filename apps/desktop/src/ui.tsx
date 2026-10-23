// Small pieces the screens share: status chips, a pack's state, dates.
import type { PackInfo } from "./api";

export function Chip({ state, children }: { state: "ok" | "busy" | "bad" | "off"; children: React.ReactNode }) {
  return <span className={`chip chip-${state}`}>{children}</span>;
}

export function PackStatus({ pack }: { pack: PackInfo | null | undefined }) {
  if (!pack) return <Chip state="busy">Pack building</Chip>;
  if (!pack.available) return <Chip state="off">Pack expired</Chip>;
  return <Chip state="ok">Pack ready{pack.prepared_answers != null ? ` (${pack.prepared_answers} answers)` : ""}</Chip>;
}

export function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
