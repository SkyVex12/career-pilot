/** Deliberate allowlist: authentication, live control, speech and operation reads stay fresh. */
export type CacheMode = "default" | "reload" | "no-store";
export type CacheEntry = {
  path: string;
  data: unknown;
  etag?: string;
  checkedAt: number;
  ttl: number;
  bytes: number;
};
export type StoredResponse = {
  data: unknown;
  etag?: string;
  savedAt: number;
  ttl?: number;
};
export interface ResponsePersistence {
  read(namespace: string, path: string): Promise<StoredResponse | null>;
  write(namespace: string, path: string, value: StoredResponse): Promise<void>;
  remove(namespace: string, path: string): Promise<void>;
  invalidate?(namespace: string, prefixes: string[]): Promise<void>;
  clear(): Promise<void>;
}
const pathOnly = (path: string) => path.split("?", 1)[0];
/** These small reconciliation reads may share a flight, but never retain its response. */
export function shareStatusRead(path: string): boolean {
  const p = pathOnly(path);
  return (
    p === "/applications/status" ||
    /^\/(?:sessions|employers)\/[^/]+\/status$/.test(p) ||
    /^\/ask\/conversations\/[^/]+\/messages\/[^/]+$/.test(p)
  );
}
export function cacheLifetime(path: string): number | null {
  const p = pathOnly(path);
  if (
    ["/sessions/live", "/sessions/active", "/applications/status"].includes(p)
  )
    return null;
  if (p === "/call-languages") return 86_400_000;
  if (p === "/profile/call-preferences") return 60_000;
  if (/^\/sessions(?:\/[^/]+(?:\/(?:transcript|replay))?)?$/.test(p))
    return p.endsWith("/transcript") || p.endsWith("/replay")
      ? 300_000
      : 30_000;
  if (
    /^\/employers(?:\/[^/]+(?:\/(?:logs|documents|memory)(?:\/[^/]+)?)?)?$/.test(
      p,
    )
  )
    return 60_000;
  if (/^\/(?:applications|resumes|interviews|documents)(?:\/[^/]+)?$/.test(p))
    return 30_000;
  if (/^\/(?:proposals|usage|meeting-preparation)$/.test(p)) return 60_000;
  // Pending conversations receive ttl=0 below so their reconciliation GET still runs.
  if (
    /^\/ask\/conversations(?:\/[^/]+)?$/.test(p) ||
    p === "/ask/context-options"
  )
    return 30_000;
  return null;
}
export function hasPendingContent(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(hasPendingContent);
  if (!value || typeof value !== "object") return false;
  const r = value as Record<string, unknown>;
  if (
    [
      "live",
      "pending",
      "queued",
      "running",
      "processing",
      "extracting",
      "ingesting",
      "indexing",
      "ending",
    ].includes(String(r.status)) ||
    ["queued", "running"].includes(String(r.operation_state))
  )
    return true;
  return Object.values(r).some(
    (v) => v !== r.status && typeof v === "object" && hasPendingContent(v),
  );
}
export function persistentResource(path: string): boolean {
  return (
    path === "/call-languages" ||
    /^\/sessions\/[^/?]+(?:\/(?:transcript|replay))?$/.test(path) ||
    /^\/employers\/[^/?]+\/(?:logs|documents)\/[^/?]+$/.test(path)
  );
}
export function mutationPrefixes(path: string): string[] {
  const p = pathOnly(path);
  if (/^\/(?:auth|profiles|career-os)(?:\/|$)/.test(p)) return ["/"];
  if (/^\/(?:applications|resumes|interviews|documents)(?:\/|$)/.test(p))
    return [
      "/applications",
      "/resumes",
      "/interviews",
      "/documents",
      "/meeting-preparation",
      "/ask/context-options",
    ];
  if (/^\/(?:employers|proposals|memory)(?:\/|$)/.test(p))
    return [
      "/employers",
      "/proposals",
      "/sessions",
      "/meeting-preparation",
      "/ask/context-options",
    ];
  if (p.startsWith("/sessions"))
    return [
      "/sessions",
      "/usage",
      "/meeting-preparation",
      "/proposals",
      "/employers",
      "/ask/context-options",
    ];
  if (p.startsWith("/calendars")) return ["/meeting-preparation"];
  if (p.startsWith("/ask/")) return ["/ask/conversations"];
  if (p.startsWith("/profile/")) return ["/profile/"];
  return [p];
}
export function matchesPrefix(path: string, prefix: string): boolean {
  return (
    prefix === "/" ||
    path === prefix ||
    path.startsWith(prefix.endsWith("/") ? prefix : prefix + "/") ||
    path.startsWith(prefix + "?")
  );
}
export function copyValue<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}
/** LRU memory budget is shared by every screen using this Api instance. */
export class ResponseCache {
  private entries = new Map<string, CacheEntry>();
  private size = 0;
  private maxBytes: number;
  private maxEntries: number;
  constructor(maxBytes = 4 * 1024 * 1024, maxEntries = 128) {
    this.maxBytes = maxBytes;
    this.maxEntries = maxEntries;
  }
  get(key: string): CacheEntry | undefined {
    const value = this.entries.get(key);
    if (value) {
      this.entries.delete(key);
      this.entries.set(key, value);
    }
    return value;
  }
  set(key: string, value: CacheEntry): void {
    this.delete(key);
    if (value.bytes > this.maxBytes) return;
    this.entries.set(key, value);
    this.size += value.bytes;
    while (this.size > this.maxBytes || this.entries.size > this.maxEntries)
      this.delete(this.entries.keys().next().value!);
  }
  delete(key: string): void {
    const previous = this.entries.get(key);
    if (previous) this.size -= previous.bytes;
    this.entries.delete(key);
  }
  invalidate(prefixes: string[]): void {
    for (const [key, value] of this.entries)
      if (prefixes.some((p) => matchesPrefix(value.path, p))) this.delete(key);
  }
  clear(): void {
    this.entries.clear();
    this.size = 0;
  }
}
