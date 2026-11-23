import { withDeadline } from "./requestScope.ts";
import { HTTP_TEST_BUILD, transportURL } from "./transport.ts";
import { CompanionAttachmentRetirements } from "./companionAttachment.ts";

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
  constructor(data: string | ArrayBuffer, contentType: string, filename: string) {
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
  profile = "";
  attachments = new Set<string>();
  companionRetirements = new CompanionAttachmentRetirements();
  constructor(
    base: string,
    store: SecretStore,
    fetcher = fetch,
    development = false,
    httpTest = HTTP_TEST_BUILD,
    requestTimeoutMs = 120000,
  ) {
    this.base = secureBase(base, development, httpTest);
    this.requestTimeoutMs = requestTimeoutMs;
    this.store = store;
    this.fetcher = fetcher;
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
    this.tokens = restored;
    return !!this.tokens;
  }
  async accept(t: Tokens) {
    const epoch = ++this.epoch;
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
    this.tokens = null;
    this.profile = "";
    for (const id of attachments) this.companionRetirements.retire(id);
    this.attachments.clear();
    this.companionRetirements = new CompanionAttachmentRetirements();
    await this.persist(null);
    if (!previous) return true;
    try {
      return await withDeadline(async signal => {
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
          (r, index) => r.ok || (index < attachments.length && r.status === 404),
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
  ): Promise<T> {
    return withDeadline(bounded => this.performRequest<T>(path, body, method, auth, bounded, retry, profile, epoch), this.requestTimeoutMs, signal);
  }
  /** Files use the same account/profile fence, refresh and deadline as JSON requests. */
  async upload<T = unknown>(path: string, data: Uint8Array | string, contentType: string, filename: string, signal?: AbortSignal, profile = this.profile): Promise<T> {
    if (!["text/plain", "text/markdown", "application/pdf"].includes(contentType))
      throw new Error("Choose a text, Markdown or PDF file.");
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    if (!bytes.byteLength || bytes.byteLength > 10 * 1024 * 1024)
      throw new Error("Choose a nonempty file of at most 10 MB.");
    const name = filename.trim();
    if (!name || name.length > 255 || /[\x00-\x1f\x7f]/.test(name))
      throw new Error("Use a file name of 1–255 characters without control characters.");
    const payload = new UploadBody(typeof data === "string" ? data : new Uint8Array(data).buffer, contentType, name);
    return this.request<T>(path, payload, "POST", true, signal, true, profile);
  }
  private async performRequest<T>(path: string, body: unknown, method: string, auth: boolean, signal: AbortSignal, retry: boolean, profile: string, epoch: number): Promise<T> {
    this.assertEpoch(epoch);
    signal.throwIfAborted();
    if (!path.startsWith("/") || path.startsWith("//"))
      throw new Error("Invalid API path");
    const headers: Record<string, string> = {
      "Content-Type": body instanceof UploadBody ? body.contentType : "application/json",
    };
    if (body instanceof UploadBody) headers["x-file-name"] = encodeURIComponent(body.filename);
    if (auth) {
      headers.Authorization = `Bearer ${await this.token()}`;
      this.assertEpoch(epoch);
      if (profile) headers["x-copilot-profile"] = profile;
    }
    signal.throwIfAborted();
    const response = await this.fetcher(`${this.base}/v1${path}`, {
      method,
      headers,
      body: body instanceof UploadBody ? body.data : body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    this.assertEpoch(epoch);
    signal.throwIfAborted();
    if (response.status === 401 && auth && retry) {
      await this.token(true);
      return this.request<T>(
        path,
        body,
        method,
        auth,
        signal,
        false,
        profile,
        epoch,
      );
    }
    if (!response.ok) {
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
      throw new ApiError(response.status, message);
    }
    const data =
      response.status === 204
        ? (undefined as T)
        : ((await response.json()) as T);
    this.assertEpoch(epoch);
    signal.throwIfAborted();
    return data;
  }
}
