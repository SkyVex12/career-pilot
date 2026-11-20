import type { Api, Tokens } from "./api";
import type { CareerOsOption } from "./authConfig";
export const CAREER_OS_REDIRECT = "http://127.0.0.1:53683/callback";
export type CareerOsAccount = {
  id?: string;
  email?: string;
  methods: string[];
  created_at: string;
};
export type CareerOsOutcome =
  | ({
      status: "signed_in";
      account: "existing" | "new";
      name?: string;
    } & Tokens)
  | { status: "added" }
  | {
      status: "choose_account";
      ticket: string;
      expires_in_s: number;
      accounts: CareerOsAccount[];
    }
  | {
      status: "existing_or_new";
      ticket: string;
      expires_in_s: number;
      verified: boolean;
      account: CareerOsAccount | null;
    };
export type PkceCrypto = {
  randomBytes(n: number): Uint8Array;
  sha256(text: string): Promise<string>;
};
const alphabet =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
function base64url(bytes: Uint8Array): string {
  let out = "",
    buffer = 0,
    bits = 0;
  for (const byte of bytes) {
    buffer = (buffer << 8) | byte;
    bits += 8;
    while (bits >= 6) {
      bits -= 6;
      out += alphabet[(buffer >>> bits) & 63];
    }
  }
  if (bits) out += alphabet[(buffer << (6 - bits)) & 63];
  return out;
}
export type CareerOsAttempt = {
  authorizeUrl: string;
  expiresAt: number;
  exchange(
    api: Api,
    callback: string,
    signal?: AbortSignal,
  ): Promise<CareerOsOutcome>;
  cancel(): void;
};
/** Manual browser callback fallback. Secrets stay in this one in-memory attempt, never storage. */
export async function beginCareerOs(
  option: CareerOsOption,
  crypto?: PkceCrypto,
  now = () => Date.now(),
): Promise<CareerOsAttempt> {
  if (!crypto) {
    const native = await import("expo-crypto");
    crypto = {
      randomBytes: (n) => native.getRandomBytes(n),
      sha256: (text) =>
        native.digestStringAsync(native.CryptoDigestAlgorithm.SHA256, text),
    };
  }
  const endpoint = new URL(option.authorize_url);
  if (
    endpoint.protocol !== "https:" ||
    endpoint.username ||
    endpoint.password ||
    endpoint.search ||
    endpoint.hash ||
    !option.client_id
  )
    throw new Error("Invalid secure Career-OS authorization endpoint.");
  if (!endpoint.pathname.replace(/\/$/, "").endsWith("/career-pilot/authorize"))
    endpoint.pathname =
      endpoint.pathname.replace(/\/$/, "") + "/career-pilot/authorize";
  let verifier = base64url(crypto.randomBytes(32));
  const state = base64url(crypto.randomBytes(24));
  const digest = await crypto.sha256(verifier);
  if (!/^[a-fA-F0-9]{64}$/.test(digest))
    throw new Error("Could not prepare secure sign-in.");
  const challenge = base64url(
    Uint8Array.from(digest.match(/../g)!, (x) => parseInt(x, 16)),
  );
  endpoint.search = new URLSearchParams({
    client_id: option.client_id,
    redirect_uri: CAREER_OS_REDIRECT,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
  }).toString();
  const expiresAt = now() + 5 * 60_000;
  let used = false;
  return {
    authorizeUrl: endpoint.toString(),
    expiresAt,
    cancel() {
      used = true;
      verifier = "";
    },
    async exchange(api, callback, signal) {
      if (used || now() >= expiresAt) {
        verifier = "";
        throw new Error("Sign-in expired or was already used. Start again.");
      }
      signal?.throwIfAborted();
      if (callback.length > 4096) throw new Error("Invalid sign-in callback.");
      const url = new URL(callback.trim());
      if (
        url.origin !== "http://127.0.0.1:53683" ||
        url.pathname !== "/callback" ||
        url.hash ||
        url.username ||
        url.password
      )
        throw new Error(
          "Paste the complete localhost callback address from this sign-in.",
        );
      for (const key of ["state", "code", "error"])
        if (url.searchParams.getAll(key).length > 1)
          throw new Error("Invalid duplicate sign-in parameters.");
      if (url.searchParams.get("state") !== state)
        throw new Error(
          "This callback belongs to a different sign-in. Use the current browser attempt.",
        );
      if (url.searchParams.has("error")) {
        used = true;
        verifier = "";
        throw new Error(
          "Browser sign-in was declined. Start again when ready.",
        );
      }
      const code = url.searchParams.get("code");
      if (!code || code.length > 200)
        throw new Error(
          "The callback does not contain a valid authorization code.",
        );
      used = true;
      const code_verifier = verifier;
      verifier = "";
      return api.request<CareerOsOutcome>(
        "/auth/career-os/sign-in",
        { code, code_verifier, redirect_uri: CAREER_OS_REDIRECT },
        "POST",
        false,
        signal,
      );
    },
  };
}
/** Existing-account completion deliberately requires an already authenticated Api as proof. */
export async function completeCareerOs(
  api: Api,
  ticket: string,
  choice: "existing" | "new",
  accountId?: string,
  signal?: AbortSignal,
): Promise<CareerOsOutcome> {
  if (!ticket || ticket.length < 20 || ticket.length > 200)
    throw new Error("Invalid or expired account-choice ticket.");
  if (choice === "new" && accountId)
    throw new Error("New account choice cannot select an existing account.");
  return api.request<CareerOsOutcome>(
    "/auth/career-os/complete",
    { ticket, choice, ...(accountId ? { account_id: accountId } : {}) },
    "POST",
    choice === "existing",
    signal,
  );
}
