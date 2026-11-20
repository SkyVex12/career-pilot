/** A lifecycle stop invalidates every pending permission/preparation/playback continuation. */
export class MediaEpoch {
  private value = 0;
  begin(): number {
    return ++this.value;
  }
  stop(): void {
    this.value++;
  }
  current(ticket: number): boolean {
    return ticket === this.value;
  }
}
