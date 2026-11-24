import { useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import type { Api } from "./api";
import { RequestScope } from "./requestScope";
import type { CacheMode } from "./dataCache";

export type WorkspaceRequest = <T = unknown>(
  path: string,
  body?: unknown,
  method?: string,
  cacheMode?: CacheMode,
) => Promise<T>;
/** A mounted workspace belongs to one profile. A late request cannot cross that boundary. */
export function useWorkspaceRequests(api: Api) {
  const profile = useRef(api.profile).current;
  const readScope = useRef(new RequestScope());
  const writeScope = useRef(new RequestScope());
  const alive = useRef(true),
    locked = useRef(false),
    readRevision = useRef(0);
  const [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [foreground, setForeground] = useState(
    AppState.currentState !== "background" &&
      AppState.currentState !== "inactive",
  );
  const foregroundRef = useRef(foreground);
  useEffect(() => {
    alive.current = true;
    const subscription = AppState.addEventListener("change", (state) => {
      foregroundRef.current = state === "active";
      setForeground(foregroundRef.current);
      if (!foregroundRef.current) cancelRead();
    });
    return () => {
      alive.current = false;
      subscription.remove();
      readScope.current.invalidate();
      writeScope.current.invalidate();
    };
  }, [api]);
  const reader =
    (signal: AbortSignal, fresh = false): WorkspaceRequest =>
    (path, body, method = "GET", cacheMode = fresh ? "reload" : "default") =>
      api.request(
        path,
        body,
        method,
        true,
        signal,
        true,
        profile,
        undefined,
        cacheMode,
      );
  async function read<T>(
    operation: (request: WorkspaceRequest, signal: AbortSignal) => Promise<T>,
    fresh = false,
  ): Promise<T | undefined> {
    if (!alive.current || !foregroundRef.current || locked.current)
      return undefined;
    const revision = ++readRevision.current;
    setLoading(true);
    setError("");
    try {
      return await readScope.current.run((signal) =>
        operation(reader(signal, fresh), signal),
      );
    } catch (e) {
      if (alive.current && revision === readRevision.current)
        setError(String(e));
      return undefined;
    } finally {
      if (alive.current && revision === readRevision.current) setLoading(false);
    }
  }
  async function mutate<T>(
    operation: (request: WorkspaceRequest, signal: AbortSignal) => Promise<T>,
  ): Promise<{ value: T } | undefined> {
    if (locked.current || !alive.current) return undefined;
    cancelRead();
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      return await writeScope.current.run(async (signal) => ({
        value: await operation(reader(signal), signal),
      }));
    } catch (e) {
      if (alive.current) setError(String(e));
      return undefined;
    } finally {
      locked.current = false;
      if (alive.current) setBusy(false);
    }
  }
  function cancelRead() {
    readRevision.current++;
    readScope.current.invalidate();
    if (alive.current) setLoading(false);
  }
  return {
    profile,
    read,
    mutate,
    cancelRead,
    loading,
    busy,
    error,
    setError,
    foreground,
  };
}
