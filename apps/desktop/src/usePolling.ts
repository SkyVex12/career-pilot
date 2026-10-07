import { useEffect } from "react";

/// Call `f` every `ms` while `active`.
export function usePolling(f: () => unknown, ms: number, active: boolean) {
  useEffect(() => {
    if (!active) return;
    const t = setInterval(f, ms);
    return () => clearInterval(t);
  }, [f, ms, active]);
}
