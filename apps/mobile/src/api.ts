import { withDeadline } from "./requestScope.ts";
import { HTTP_TEST_BUILD, transportURL } from "./transport.ts";
import { CompanionAttachmentRetirements } from "./companionAttachment.ts";
import {
  ResponseCache,
  cacheLifetime,
  copyValue,
  hasPendingContent,
  matchesPrefix,
  mutationPrefixes,
  persistentResource,
  shareStatusRead,
  type CacheEntry,
  type CacheMode,
  type ResponsePersistence,
} from "./dataCache.ts";

export interface Tokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  email: string;
  expires_at?: number;
}
export interface SecretStore {
  read(): Promise<Tokens | null>;
  write(tokens: Tokens | null): Promise<void>;
}
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}
class UploadBody {
  readonly data: string | ArrayBuffer;
  readonly contentType: string;
  readonly filename: string;
  constructor(
    data: string | ArrayBuffer,
    contentType: string,
    filename: string,
  ) {
    this.data = data;
    this.contentType = contentType;
    this.filename = filename;
  }
}
export function secureBase(
  raw: string,
  development = false,
  httpTest = HTTP_TEST_BUILD,
): string {
  const u = transportURL(raw, "rest", development, httpTest);
  if (!["/", "/v1", "/v1/"].includes(u.pathname))
    throw new Error(
      "Use the server origin, optionally ending in /v1; other paths are not supported.",
    );
  return u.origin;
}
export class Api {
  readonly base: string;
  private store: SecretStore;
  private fetcher: typeof fetch;
  private tokens: Tokens | null = null;
  private refreshing: { epoch: number; promise: Promise<void> } | null = null;
  private epoch = 0;
  private requestTimeoutMs: number;
  private writes: Promise<void> = Promise.resolve();
  private selectedProfile = "";
  private contextRevision = 0;
  private dataRevision = 0;
  private invalidations: { revision: number; prefixes: string[] }[] = [];
  private oldestRevision = 0;
  private cache = new ResponseCache();
  private pending = new Map<
    string,
    {
      controller: AbortController;
      promise: Promise<unknown>;
      consumers: number;
    }
  >();
  private canonicalScopes = new Map<string, string>();
  private syncCursors = new Map<
    string,
    { cursor?: string; checkedAt: number }
  >();
  private syncPending = new Map<string, Promise<void>>();
  private persistence?: ResponsePersistence;
  readonly cacheStats = { hits: 0, misses: 0, shared: 0, unchanged: 0 };
  get profile() {
    return this.selectedProfile;
  }
  set profile(value: string) {
    if (value === this.selectedProfile) return;
    this.selectedProfile = value;
    this.resetCache();
  }
  attachments = new Set<string>();
  companionRetirements = new CompanionAttachmentRetirements();
  constructor(
    base: string,
    store: SecretStore,
    fetcher = fetch,
    development = false,
    httpTest = HTTP_TEST_BUILD,
    requestTimeoutMs = 120000,
    persistence?: ResponsePersistence,
  ) {
    this.base = secureBase(base, development, httpTest);
    this.requestTimeoutMs = requestTimeoutMs;
    this.store = store;
    this.fetcher = fetcher;
    this.persistence = persistence;
  }
  private resetCache() {
    this.contextRevision++;
    this.cache.clear();
    this.canonicalScopes.clear();
    this.syncCursors.clear();
    this.syncPending.clear();
    for (const value of this.pending.values()) value.controller.abort();
    this.pending.clear();
  }
  /** Explicit refresh and successful mutations invalidate only related resource groups. */
  invalidateCache(prefixes: string[] = ["/"]) {
    this.dataRevision++;
    this.invalidations.push({ revision: this.dataRevision, prefixes });
    if (this.invalidations.length > 128)
      this.oldestRevision = this.invalidations.shift()!.revision;
    this.cache.invalidate(prefixes);
    for (const [key, pending] of this.pending) {
      const path = key.slice(key.indexOf("\n") + 1);
      if (prefixes.some((prefix) => matchesPrefix(path, prefix))) {
        pending.controller.abort();
        this.pending.delete(key);
      }
    }
  }
  private invalidatedSince(path: string, revision: number): boolean {
    return (
      revision < this.oldestRevision ||
      this.invalidations.some(
        (change) =>
          change.revision > revision &&
          change.prefixes.some((prefix) => matchesPrefix(path, prefix)),
      )
    );
  }
  /** Foreground-only delta check. A bootstrap/reset discards old representations safely. */
  async syncDomain(
    domain: "history" | "work",
    signal?: AbortSignal,
    force = false,
  ): Promise<void> {
    const key = `${this.profile}:${domain}`,
      previous = this.syncCursors.get(key);
    if (!force && previous && Date.now() - previous.checkedAt < 30_000) return;
    const existing = this.syncPending.get(key);
    if (existing)
      return withDeadline(() => existing, this.requestTimeoutMs, signal);
    const context = this.contextRevision;
    const promise = (async () => {
      let cursor = previous?.cursor,
        pages = 0;
      do {
        const result = await this.request<{
          reset: boolean;
          cursor: string;
          changes: { resource: string; id: string; deleted: boolean }[];
          has_more: boolean;
        }>(
          `/sync?domain=${domain}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
          undefined,
          "GET",
          true,
          signal,
        );
        if (context !== this.contextRevision) return;
        if (result.reset || result.changes?.length) {
          // Collection responses include child counts and derived content. Domain invalidation
          // also covers these dependencies and deletions before the next screen read.
          this.invalidateCache(
            domain === "history"
              ? ["/sessions", "/usage", "/meeting-preparation"]
              : [
                  "/employers",
                  "/proposals",
                  "/sessions",
                  "/ask/context-options",
                  "/meeting-preparation",
                ],
          );
          const namespace = this.canonicalScopes.get(this.profile);
          if (
            namespace &&
            (previous?.cursor || result.changes?.length) &&
            this.persistence?.invalidate
          ) {
            const prefixes =
              domain === "work"
                ? ["/employers", "/sessions"]
                : result.reset ||
                    result.changes.some(
                      (change) => change.resource === "references",
                    )
                  ? ["/sessions"]
                  : result.changes.map(
                      (change) => `/sessions/${encodeURIComponent(change.id)}`,
                    );
            await this.persistence
              .invalidate(namespace, prefixes)
              .catch(() => {});
            if (context !== this.contextRevision) return;
          }
        }
        cursor = result.cursor;
        if (!result.has_more) {
          this.syncCursors.set(key, { cursor, checkedAt: Date.now() });
          return;
        }
      } while (++pages < 20);
      // Never advance past an incomplete drain: retry from the last fully processed page.
      this.syncCursors.set(key, { cursor, checkedAt: 0 });
    })().catch((error) => {
      // Older compatible servers still work through bounded TTL/ETag revalidation.
      if (!(
        error instanceof ApiError &&
        (error.status === 404 || error.status === 405)
      ))
        throw error;
      this.syncCursors.set(key, { checkedAt: Date.now() });
    });
    this.syncPending.set(key, promise);
    try {
      await promise;
    } finally {
      if (this.syncPending.get(key) === promise) this.syncPending.delete(key);
    }
  }
  private persist(tokens: Tokens | null): Promise<void> {
    // Serialize keychain writes: a refresh already persisting must finish before logout clears it.
    this.writes = this.writes
      .catch(() => {})
      .then(() => this.store.write(tokens));
    return this.writes;
  }
  async restore() {
    const epoch = this.epoch;
    await this.writes;
    const restored = await this.store.read();
    this.assertEpoch(epoch);
    this.resetCache();
    this.tokens = restored;
    return !!this.tokens;
  }
  async accept(t: Tokens) {
    const epoch = ++this.epoch;
    this.resetCache();
    this.attachments.clear();
    this.companionRetirements = new CompanionAttachmentRetirements();
    this.tokens = { ...t, expires_at: Date.now() + t.expires_in * 1000 };
    await this.persist(this.tokens);
    this.assertEpoch(epoch);
  }
  get email() {
    return this.tokens?.email ?? "";
  }
  private assertEpoch(epoch: number) {
    if (epoch !== this.epoch)
      throw new ApiError(
        401,
        "Account changed. Retry from the current account.",
      );
  }
  async token(force = false): Promise<string> {
    const epoch = this.epoch;
    if (!this.tokens) throw new ApiError(401, "Sign in to continue.");
    if (force || (this.tokens.expires_at ?? 0) < Date.now() + 60000)
      await this.refresh();
    this.assertEpoch(epoch);
    if (!this.tokens) throw new ApiError(401, "Sign in again.");
    return this.tokens.access_token;
  }
  private async refresh() {
    if (this.refreshing?.epoch === this.epoch) return this.refreshing.promise;
    const epoch = this.epoch,
      token = this.tokens?.refresh_token;
    if (!token) throw new ApiError(401, "Sign in again.");
    const entry = { epoch, promise: Promise.resolve() };
    this.refreshing = entry;
    entry.promise = (async () => {
      try {
        const data = await this.request<Tokens>(
          "/auth/token/refresh",
          { refresh_token: token },
          "POST",
          false,
        );
        if (epoch !== this.epoch) return;
        this.tokens = {
          ...data,
          expires_at: Date.now() + data.expires_in * 1000,
        };
        await this.persist(this.tokens);
      } catch (e) {
        if (epoch === this.epoch && e instanceof ApiError && e.status === 401) {
          this.tokens = null;
          this.resetCache();
          void this.persistence?.clear().catch(() => {});
          await this.persist(null);
        }
        throw e;
      } finally {
        if (this.refreshing === entry) this.refreshing = null;
      }
    })();
    return entry.promise;
  }
  async signout(): Promise<boolean> {
    const previous = this.tokens,
      attachments = [...this.attachments],
      profile = this.profile;
    this.epoch++;
    this.resetCache();
    this.tokens = null;
    this.profile = "";
    for (const id of attachments) this.companionRetirements.retire(id);
    this.attachments.clear();
    this.companionRetirements = new CompanionAttachmentRetirements();
    await this.persistence?.clear().catch(() => {});
    await this.persist(null);
    if (!previous) return true;
    try {
      return await withDeadline(async (signal) => {
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
          Authorization: `Bearer ${previous.access_token}`,
        };
        if (profile) headers["x-copilot-profile"] = profile;
        const results = await Promise.all([
          ...attachments.map((id) =>
            this.fetcher(`${this.base}/v1/companion/attachments/${id}`, {
              method: "DELETE",
              headers,
              signal,
            }),
          ),
          this.fetcher(`${this.base}/v1/auth/token/signout`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: previous.refresh_token }),
            signal,
          }),
        ]);
        // A missing attachment is already detached. A missing token endpoint does
        // not confirm refresh-token revocation.
        return results.every(
          (r, index) =>
            r.ok || (index < attachments.length && r.status === 404),
        );
      }, 8000);
    } catch {
      return false;
    }
  }
  async request<T = unknown>(
    path: string,
    body?: unknown,
    method = "GET",
    auth = true,
    signal?: AbortSignal,
    retry = true,
    profile = this.profile,
    epoch = this.epoch,
    cacheMode: CacheMode = "default",
  ): Promise<T> {
    this.assertEpoch(epoch);
    const ttl =
      method === "GET" && auth && body === undefined && cacheMode !== "no-store"
        ? cacheLifetime(path)
        : null;
    const sharedStatus =
      method === "GET" && auth && body === undefined && shareStatusRead(path);
    if (ttl === null && !sharedStatus) {
      const value = await withDeadline(
        (bounded) =>
          this.performRequest<T>(
            path,
            body,
            method,
            auth,
            bounded,
            retry,
            profile,
            epoch,
          ),
        this.requestTimeoutMs,
        signal,
      );
      if (auth && !["GET", "HEAD", "OPTIONS"].includes(method)) {
        const prefixes = mutationPrefixes(path),
          namespace = this.canonicalScopes.get(profile);
        this.invalidateCache(prefixes);
        // Include persisted entries not loaded this launch, while retaining unrelated history.
        if (
          namespace &&
          !prefixes.includes("/") &&
          this.persistence?.invalidate
        )
          await this.persistence
            .invalidate(namespace, prefixes)
            .catch(() => {});
        else await this.persistence?.clear().catch(() => {});
        this.assertEpoch(epoch);
      }
      return value;
    }
    const context = this.contextRevision,
      revision = this.dataRevision;
    if (profile !== this.profile) {
      const error = new Error("Workspace changed.");
      error.name = "AbortError";
      throw error;
    }
    const key = `${epoch}:${profile}\n${path}`;
    const assertContext = () => {
      this.assertEpoch(epoch);
      if (
        context !== this.contextRevision ||
        this.invalidatedSince(path, revision)
      ) {
        const error = new Error(
          "Data changed while loading. Refresh to continue.",
        );
        error.name = "AbortError";
        throw error;
      }
    };
    signal?.throwIfAborted();
    const cached = this.cache.get(key);
    if (
      cached &&
      cacheMode === "default" &&
      Date.now() - cached.checkedAt < cached.ttl
    ) {
      this.cacheStats.hits++;
      return copyValue(cached.data as T);
    }
    let pending = this.pending.get(key);
    if (!pending) {
      this.cacheStats.misses++;
      const controller = new AbortController();
      const entry = {
        controller,
        consumers: 0,
        promise: Promise.resolve(undefined as unknown),
      };
      entry.promise = withDeadline(
        async (bounded) => {
          let candidate = cached;
          const namespace =
            path === "/call-languages"
              ? `${this.base}:catalog:v1`
              : this.canonicalScopes.get(profile);
          if (!candidate && namespace && persistentResource(path)) {
            const persisted = await this.persistence
              ?.read(namespace, path)
              .catch(() => null);
            assertContext();
            bounded.throwIfAborted();
            if (persisted) {
              candidate = {
                path,
                ...persisted,
                checkedAt: 0,
                ttl: ttl ?? 0,
                bytes: JSON.stringify(persisted.data).length * 2,
              };
              // Only the public catalog can be used offline across launches. Private content
              // always requires an authorized server validation after restart.
              if (
                path === "/call-languages" &&
                cacheMode === "default" &&
                Date.now() - persisted.savedAt <
                  Math.min(ttl ?? 0, persisted.ttl ?? 0)
              ) {
                candidate.checkedAt = persisted.savedAt;
                candidate.ttl = Math.min(ttl ?? 0, persisted.ttl ?? 0);
                this.cache.set(key, candidate);
                return candidate.data;
              }
            }
          }
          const response = await this.performRequest<T>(
            path,
            body,
            method,
            auth,
            bounded,
            retry,
            profile,
            epoch,
            candidate,
            ttl !== null,
          );
          assertContext();
          bounded.throwIfAborted();
          return response;
        },
        this.requestTimeoutMs,
        controller.signal,
      ).finally(() => {
        if (this.pending.get(key) === entry) this.pending.delete(key);
      });
      this.pending.set(key, entry);
      pending = entry;
    } else this.cacheStats.shared++;
    pending.consumers++;
    try {
      const value = await withDeadline(
        () => pending!.promise,
        this.requestTimeoutMs,
        signal,
      );
      assertContext();
      return copyValue(value as T);
    } finally {
      pending.consumers--;
      if (!pending.consumers && this.pending.get(key) === pending) {
        pending.controller.abort();
        this.pending.delete(key);
      }
    }
  }
  /** Files use the same account/profile fence, refresh and deadline as JSON requests. */
  async upload<T = unknown>(
    path: string,
    data: Uint8Array | string,
    contentType: string,
    filename: string,
    signal?: AbortSignal,
    profile = this.profile,
  ): Promise<T> {
    if (
      !["text/plain", "text/markdown", "application/pdf"].includes(contentType)
    )
      throw new Error("Choose a text, Markdown or PDF file.");
    const bytes =
      typeof data === "string" ? new TextEncoder().encode(data) : data;
    if (!bytes.byteLength || bytes.byteLength > 10 * 1024 * 1024)
      throw new Error("Choose a nonempty file of at most 10 MB.");
    const name = filename.trim();
    if (!name || name.length > 255 || /[\x00-\x1f\x7f]/.test(name))
      throw new Error(
        "Use a file name of 1–255 characters without control characters.",
      );
    const payload = new UploadBody(
      typeof data === "string" ? data : new Uint8Array(data).buffer,
      contentType,
      name,
    );
    return this.request<T>(path, payload, "POST", true, signal, true, profile);
  }
  private async performRequest<T>(
    path: string,
    body: unknown,
    method: string,
    auth: boolean,
    signal: AbortSignal,
    retry: boolean,
    profile: string,
    epoch: number,
    cached?: CacheEntry,
    allowCache = false,
  ): Promise<T> {
    const context = this.contextRevision,
      revision = this.dataRevision;
    this.assertEpoch(epoch);
    signal.throwIfAborted();
    if (!path.startsWith("/") || path.startsWith("//"))
      throw new Error("Invalid API path");
    const headers: Record<string, string> = {
      "Content-Type":
        body instanceof UploadBody ? body.contentType : "application/json",
    };
    if (body instanceof UploadBody)
      headers["x-file-name"] = encodeURIComponent(body.filename);
    if (cached?.etag) headers["If-None-Match"] = cached.etag;
    if (auth) {
      headers.Authorization = `Bearer ${await this.token()}`;
      this.assertEpoch(epoch);
      if (profile) headers["x-copilot-profile"] = profile;
    }
    signal.throwIfAborted();
    const response = await this.fetcher(`${this.base}/v1${path}`, {
      method,
      headers,
      body:
        body instanceof UploadBody
          ? body.data
          : body === undefined
            ? undefined
            : JSON.stringify(body),
      signal,
    });
    this.assertEpoch(epoch);
    signal.throwIfAborted();
    if (response.status === 401 && auth && retry) {
      await this.token(true);
      return this.performRequest<T>(
        path,
        body,
        method,
        auth,
        signal,
        false,
        profile,
        epoch,
        cached,
        allowCache,
      );
    }
    const key = `${epoch}:${profile}\n${path}`;
    const account = response.headers.get("X-Copilot-Account"),
      workspace = response.headers.get("X-Copilot-Workspace");
    if (auth && account && workspace && context === this.contextRevision) {
      const namespace = JSON.stringify([
          this.base,
          account,
          workspace,
          "responses-v1",
        ]),
        old = this.canonicalScopes.get(profile);
      if (old && old !== namespace) this.resetCache();
      this.canonicalScopes.set(profile, namespace);
    }
    if (response.status === 304 && cached) {
      const control = response.headers.get("Cache-Control") ?? "";
      const maxAge = /(?:^|,)\s*max-age=(\d+)/i.exec(control);
      const ttl = /\bno-cache\b/i.test(control)
        ? 0
        : Math.min(
            cacheLifetime(path) ?? 0,
            maxAge ? Number(maxAge[1]) * 1000 : cached.ttl,
          );
      if (
        context === this.contextRevision &&
        !this.invalidatedSince(path, revision)
      ) {
        this.cacheStats.unchanged++;
        if (/\bno-store\b/i.test(control)) {
          this.cache.delete(key);
          const namespace =
            path === "/call-languages"
              ? `${this.base}:catalog:v1`
              : this.canonicalScopes.get(profile);
          if (namespace)
            void this.persistence?.remove(namespace, path).catch(() => {});
        } else this.cache.set(key, { ...cached, ttl, checkedAt: Date.now() });
      }
      return cached.data as T;
    }
    if (!response.ok) {
      this.cache.delete(key);
      if ([401, 403, 404].includes(response.status)) {
        const namespace = this.canonicalScopes.get(profile);
        if (namespace)
          void this.persistence?.remove(namespace, path).catch(() => {});
      }
      let message = `Request failed (${response.status})`;
      try {
        const data = (await response.json()) as {
          detail?: string | { message?: string };
        };
        message =
          typeof data.detail === "string"
            ? data.detail
            : (data.detail?.message ?? message);
      } catch {
        /* generic error */
      }
      if (response.status === 422 && /language/i.test(message)) {
        this.invalidateCache(["/call-languages"]);
        void this.persistence
          ?.remove(`${this.base}:catalog:v1`, "/call-languages")
          .catch(() => {});
      }
      throw new ApiError(response.status, message);
    }
    const data =
      response.status === 204
        ? (undefined as T)
        : ((await response.json()) as T);
    this.assertEpoch(epoch);
    signal.throwIfAborted();
    const ttl =
      allowCache && method === "GET" && auth ? cacheLifetime(path) : null;
    const cacheControl = response.headers.get("Cache-Control") ?? "";
    if (
      ttl !== null &&
      context === this.contextRevision &&
      !this.invalidatedSince(path, revision) &&
      !/\bno-store\b/i.test(cacheControl)
    ) {
      const etag = response.headers.get("ETag") ?? undefined,
        checkedAt = Date.now();
      const maxAge = /(?:^|,)\s*max-age=(\d+)/i.exec(cacheControl);
      const lifetime =
        hasPendingContent(data) || /\bno-cache\b/i.test(cacheControl)
          ? 0
          : Math.min(ttl, maxAge ? Number(maxAge[1]) * 1000 : ttl);
      const bytes = JSON.stringify(data)?.length * 2 || 0;
      this.cache.set(key, {
        path,
        data,
        etag,
        checkedAt,
        ttl: lifetime,
        bytes,
      });
      const namespace =
        path === "/call-languages"
          ? `${this.base}:catalog:v1`
          : this.canonicalScopes.get(profile);
      if (
        namespace &&
        persistentResource(path) &&
        (lifetime > 0 || etag) &&
        !hasPendingContent(data)
      ) {
        if (path !== "/call-languages" || lifetime > 0)
          void this.persistence
            ?.write(namespace, path, {
              data,
              etag,
              savedAt: checkedAt,
              ttl: lifetime,
            })
            .catch(() => {});
      }
    } else if (/\bno-store\b/i.test(cacheControl)) {
      this.cache.delete(key);
      const namespace =
        path === "/call-languages"
          ? `${this.base}:catalog:v1`
          : this.canonicalScopes.get(profile);
      if (namespace)
        void this.persistence?.remove(namespace, path).catch(() => {});
    }
    return data;
  }
}
