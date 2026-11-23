/** Serialize the process-wide native audio session. A late old-room cleanup must not
 * stop a newer assistant room after profile/navigation changes. */
export class AssistantAudioLease {
  private queue: Promise<void> = Promise.resolve();
  private owner: object | null = null;
  private startNative: () => Promise<void>;
  private stopNative: () => Promise<void>;
  constructor(start: () => Promise<void>, stop: () => Promise<void>) {
    this.startNative = start;
    this.stopNative = stop;
  }
  private run(action: () => Promise<void>) {
    const next = this.queue.catch(() => {}).then(action);
    this.queue = next;
    return next;
  }
  start(owner: object, valid: () => boolean) {
    return this.run(async () => {
      if (!valid()) return;
      if (this.owner && this.owner !== owner) await this.stopNative();
      this.owner = owner;
      try {
        await this.startNative();
        if (!valid() && this.owner === owner) {
          this.owner = null;
          await this.stopNative();
        }
      } catch (e) {
        if (this.owner === owner) {
          this.owner = null;
          await this.stopNative().catch(() => {});
        }
        throw e;
      }
    });
  }
  quiesce() {
    return this.run(async () => {
      if (!this.owner) return;
      this.owner = null;
      await this.stopNative();
    });
  }
  stop(owner: object) {
    return this.run(async () => {
      if (this.owner !== owner) return;
      this.owner = null;
      await this.stopNative();
    });
  }
}
