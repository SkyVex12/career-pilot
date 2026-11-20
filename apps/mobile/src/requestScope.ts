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
