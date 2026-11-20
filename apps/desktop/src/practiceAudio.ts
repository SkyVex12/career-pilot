// Half-duplex practice: playback is stopped before microphone-only recording begins.
// No desktop loopback or call capture is used here.
export class PracticeAudio {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private player: HTMLAudioElement | null = null;
  private url: string | null = null;
  private chunks: Blob[] = [];
  private generation = 0;
  private size = 0;
  private timeout: ReturnType<typeof setTimeout> | null = null;
  onPlaybackEnd: () => void = () => {};
  onInterrupted: (reason: string) => void = () => {};
  stopPlayback() { this.player?.pause(); this.player = null; if (this.url) URL.revokeObjectURL(this.url); this.url = null; }
  async speak(base64: string, mime: string) {
    this.stop();
    const generation = this.generation;
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    this.url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const audio = new Audio(this.url); this.player = audio;
    audio.onended = () => { if (generation === this.generation) { this.stopPlayback(); this.onPlaybackEnd(); } };
    await audio.play();
  }
  async start() {
    this.stop(); const generation = this.generation;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") throw new Error("Microphone recording is unavailable here. Use text instead.");
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
    if (generation !== this.generation) { stream.getTracks().forEach(t => t.stop()); return false; }
    this.stream = stream; this.chunks = []; this.size = 0;
    for (const track of stream.getTracks()) track.onended = () => { this.stop(); this.onInterrupted("Microphone disconnected. Your typed answer is preserved."); };
    const mimeType = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find(t => MediaRecorder.isTypeSupported(t));
    let recorder: MediaRecorder;
    try { recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined); } catch (error) { this.release(); throw error; }
    this.recorder = recorder;
    recorder.ondataavailable = e => { this.size += e.data.size; if (this.size > 8 * 1024 * 1024) { this.stop(); this.onInterrupted("Recording reached its size limit. Try a shorter answer or use text."); } else this.chunks.push(e.data); };
    recorder.onerror = () => { this.stop(); this.onInterrupted("Microphone recording failed. Use text or try again."); };
    try { recorder.start(1000); } catch (error) { this.release(); throw error; }
    this.timeout = setTimeout(() => { this.stop(); this.onInterrupted("Recording stopped after five minutes. Try a shorter answer or use text."); }, 300000);
    return true;
  }
  async finish(): Promise<Blob | null> {
    const recorder = this.recorder;
    if (!recorder || recorder.state === "inactive") return null;
    if (this.timeout) clearTimeout(this.timeout); this.timeout = null;
    return new Promise(resolve => { recorder.onstop = () => { const blob = new Blob(this.chunks, { type: recorder.mimeType }); this.chunks = []; this.release(); resolve(blob); }; recorder.stop(); });
  }
  private release() { this.stream?.getTracks().forEach(t => { t.onended = null; t.stop(); }); this.stream = null; this.recorder = null; }
  stop() { this.generation++; this.stopPlayback(); if (this.timeout) clearTimeout(this.timeout); this.timeout = null; if (this.recorder?.state === "recording") { this.recorder.ondataavailable = null; this.recorder.onstop = null; this.recorder.stop(); } this.release(); this.chunks = []; }
}
export const audioBase64 = (blob: Blob) => new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("Could not read microphone recording")); reader.readAsDataURL(blob); });
// Call starts await this barrier, so playback and capture stop before native capture opens.
let pausePractice: (() => Promise<void>) | null = null;
export function registerPracticePause(callback: () => Promise<void>) { pausePractice = callback; return () => { if (pausePractice === callback) pausePractice = null; }; }
export async function prepareForRealCall() { await pausePractice?.(); }
