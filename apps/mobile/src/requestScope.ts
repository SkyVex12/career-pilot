/** Only the latest request may publish. Invalidation also covers back, delete and unmount. */
export class RequestScope {
  private generation = 0;
  private controller: AbortController | null = null;
  invalidate() {
    this.generation++;
    this.controller?.abort();
    this.controller = null;
  }
  async run<T>(
    operation: (signal: AbortSignal) => Promise<T>,
  ): Promise<T | undefined> {
    this.invalidate();
    const generation = this.generation;
    const controller = new AbortController();
    this.controller = controller;
    try {
      const result = await operation(controller.signal);
      return generation === this.generation ? result : undefined;
    } catch (error) {
      if (generation === this.generation) throw error;
      return undefined;
    } finally {
      if (generation === this.generation) this.controller = null;
    }
  }
}

/** Bound the whole operation, including token acquisition and response parsing.
 * The race also settles when a native transport ignores AbortSignal. */
export async function withDeadline<T>(operation: (signal: AbortSignal) => Promise<T>, milliseconds: number, parent?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let rejectAbort: (error: Error) => void = () => {};
  const failure = (timeout: boolean) => {
    const error = new Error(timeout ? "The connection timed out. Check your connection and try again." : "Operation cancelled.");
    error.name = timeout ? "TimeoutError" : "AbortError";
    controller.abort(); rejectAbort(error);
  };
  const cancel = () => failure(false);
  const aborted = new Promise<never>((_, reject) => { rejectAbort = reject; });
  parent?.addEventListener("abort", cancel, { once: true });
  if (parent?.aborted) cancel();
  else timer = setTimeout(() => failure(true), milliseconds);
  try {
    return await Promise.race([aborted, (async () => {
      controller.signal.throwIfAborted();
      return operation(controller.signal);
    })()]);
  } finally { if (timer) clearTimeout(timer); parent?.removeEventListener("abort", cancel); }
}

/** Serial connection attempts; invalidation prevents a pending token lookup opening a stale socket. */
export class ConnectionGate {
  private revision = 0;
  private pending = false;
  begin(): number | null { if (this.pending) return null; this.pending = true; return ++this.revision; }
  current(ticket: number): boolean { return ticket === this.revision; }
  finish(ticket: number): void { if (this.current(ticket)) this.pending = false; }
  invalidate(): void { this.revision++; this.pending = false; }
}
