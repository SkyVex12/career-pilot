import { withDeadline } from "./requestScope.ts";

/** API contract revisions only. Provider credentials, reverse-proxy routing, selected
 * languages and speech quality must still be checked by their actual feature requests. */
export const CLIENT_CONTRACTS = {
  companion: 1,
  ask: 1,
  practice: 1,
  preparation: 1,
  languages: 1,
} as const;
export type ClientFeature = keyof typeof CLIENT_CONTRACTS;
export type ClientCapabilityCheck = {
  state:
    | "compatible"
    | "upgrade_required"
    | "app_update_required"
    | "unverified"
    | "signin_required";
  notice: string | null;
  contracts: Partial<Record<ClientFeature, number>>;
  serverVersion?: string;
};
type CapabilityRequester = {
  request<T = unknown>(
    path: string,
    body?: unknown,
    method?: string,
    auth?: boolean,
    signal?: AbortSignal,
    retry?: boolean,
  ): Promise<T>;
};

const featureNames: Record<ClientFeature, string> = {
  companion: "live companion",
  ask: "Ask",
  practice: "interview practice",
  preparation: "meeting preparation",
  languages: "call languages",
};
const olderServer =
  "This server needs an update for this app's latest features. Update the Career Pilot server, then reconnect.";
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

function unavailable(
  notice: string,
  state: ClientCapabilityCheck["state"] = "unverified",
): ClientCapabilityCheck {
  return { state, notice, contracts: {} };
}

/** Never treats an arbitrary successful JSON response or missing contract as support. */
export function parseClientCapabilities(value: unknown): ClientCapabilityCheck {
  if (!object(value) || !Number.isSafeInteger(value.schema_version))
    return unavailable(
      "This server's compatibility response could not be read. Reconnect after checking the server version.",
    );
  if (value.schema_version !== 1) {
    return Number(value.schema_version) > 1
      ? unavailable(
          "This server uses a newer app connection format. Update the Career Pilot mobile app, then reconnect.",
          "app_update_required",
        )
      : unavailable(olderServer, "upgrade_required");
  }
  if (!object(value.contracts))
    return unavailable(
      "This server did not report its feature compatibility. Update the server, then reconnect.",
    );
  const contracts: Partial<Record<ClientFeature, number>> = {};
  for (const feature of Object.keys(CLIENT_CONTRACTS) as ClientFeature[]) {
    const revision = value.contracts[feature];
    if (
      revision !== undefined &&
      (!Number.isSafeInteger(revision) || Number(revision) < 0)
    )
      return unavailable(
        "This server's feature compatibility response is invalid. Update the server, then reconnect.",
      );
    if (typeof revision === "number") contracts[feature] = revision;
  }
  const missing = (Object.keys(CLIENT_CONTRACTS) as ClientFeature[]).filter(
    (feature) => (contracts[feature] ?? 0) < CLIENT_CONTRACTS[feature],
  );
  const result: ClientCapabilityCheck = {
    state: missing.length ? "upgrade_required" : "compatible",
    notice: missing.length
      ? `Update the Career Pilot server to use ${missing.map((feature) => featureNames[feature]).join(", ")} with this app, then reconnect.`
      : null,
    contracts,
  };
  if (
    typeof value.server_version === "string" &&
    value.server_version.length > 0 &&
    value.server_version.length <= 80
  )
    result.serverVersion = value.server_version;
  return result;
}

/** Whether the server explicitly advertised this contract, not feature runtime readiness. */
export function supportsClientContract(
  check: ClientCapabilityCheck,
  feature: ClientFeature,
): boolean {
  return (check.contracts[feature] ?? 0) >= CLIENT_CONTRACTS[feature];
}

/** One authenticated endpoint request, no retry/poll, at most four seconds including
 * token lookup and JSON parsing. Cancellation must remain invisible after account change. */
export async function checkClientCapabilities(
  api: CapabilityRequester,
  signal?: AbortSignal,
  timeoutMs = 4000,
): Promise<ClientCapabilityCheck> {
  const deadline = Number.isFinite(timeoutMs)
    ? Math.min(4000, Math.max(1, timeoutMs))
    : 4000;
  try {
    return parseClientCapabilities(
      await withDeadline(
        (bounded) =>
          api.request(
            "/client-capabilities",
            undefined,
            "GET",
            true,
            bounded,
            false,
          ),
        deadline,
        signal,
      ),
    );
  } catch (error) {
    if (
      signal?.aborted ||
      (error instanceof Error && error.name === "AbortError")
    )
      throw error;
    const status =
      object(error) && typeof error.status === "number"
        ? error.status
        : undefined;
    if (status === 404) return unavailable(olderServer, "upgrade_required");
    if (status === 401)
      return unavailable(
        "Sign in again to check this server's compatibility.",
        "signin_required",
      );
    if (status === 426)
      return unavailable(
        "This server requires a newer mobile app. Update Career Pilot, then reconnect.",
        "app_update_required",
      );
    if (status === 403)
      return unavailable(
        "This account cannot check server compatibility. Check its access before retrying.",
      );
    if (error instanceof Error && error.name === "TimeoutError")
      return unavailable(
        "The server is taking too long to check compatibility. You can retry when the connection improves.",
      );
    return unavailable(
      "Server compatibility could not be checked. Reconnect when the server is available.",
    );
  }
}
