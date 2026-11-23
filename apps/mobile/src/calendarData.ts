import { list, nullableText, record, text } from "./workspaceData.ts";
import type { PreparationData } from "./historyData.ts";

export const PROVIDERS = ["google", "outlook"] as const;
export type CalendarProvider = (typeof PROVIDERS)[number];
export type CalendarStatus =
  "disconnected" | "pending" | "active" | "reauth_required" | "error";
export type CalendarConnection = {
  provider: CalendarProvider;
  available: boolean;
  connected: boolean;
  status: CalendarStatus;
  calendars: { id: string; name: string }[];
  selected_calendars: string[];
  last_sync_at: string | null;
  last_error: string | null;
};
export type CalendarChange = {
  provider: CalendarProvider;
  kind: "connected" | "selection" | "sync" | "disconnected" | "unavailable";
};
export const providerName = (provider: CalendarProvider) =>
  provider === "google" ? "Google Calendar" : "Outlook Calendar";
const boolean = (value: unknown) => {
  if (typeof value !== "boolean")
    throw new Error("The server returned an invalid calendar status.");
  return value;
};
const identifier = (value: unknown) => {
  const id = text(value);
  if (!id || id.length > 2000)
    throw new Error("The server returned an invalid calendar identifier.");
  return id;
};
export function parseCalendarConnections(value: unknown): CalendarConnection[] {
  const connections = list(record(value).items, (value) => {
    const r = record(value);
    if (
      !PROVIDERS.includes(r.provider as CalendarProvider) ||
      ![
        "disconnected",
        "pending",
        "active",
        "reauth_required",
        "error",
      ].includes(String(r.status))
    )
      throw new Error("Unrecognized calendar provider or connection status.");
    const calendars = list(r.calendars, (value) => {
      const calendar = record(value);
      return { id: identifier(calendar.id), name: text(calendar.name) };
    });
    if (new Set(calendars.map((c) => c.id)).size !== calendars.length)
      throw new Error("The server returned duplicate calendars.");
    const selected_calendars = list(r.selected_calendars, identifier);
    if (
      selected_calendars.length > 10 ||
      new Set(selected_calendars).size !== selected_calendars.length
    )
      throw new Error("The server returned an invalid calendar selection.");
    const last_sync_at = nullableText(r.last_sync_at);
    if (last_sync_at && !Number.isFinite(Date.parse(last_sync_at)))
      throw new Error("The server returned an invalid calendar sync date.");
    return {
      provider: r.provider as CalendarProvider,
      available: boolean(r.available),
      connected: boolean(r.connected),
      status: r.status as CalendarStatus,
      calendars,
      selected_calendars,
      last_sync_at,
      last_error: nullableText(r.last_error),
    };
  });
  if (
    connections.length !== 2 ||
    new Set(connections.map((c) => c.provider)).size !== 2
  )
    throw new Error(
      "The server did not return both calendar providers. Refresh and try again.",
    );
  return [...connections].sort(
    (a, b) => PROVIDERS.indexOf(a.provider) - PROVIDERS.indexOf(b.provider),
  );
}

/** Authorization opens externally, never inside a webview or an arbitrary server URL. */
export function calendarAuthorizationUrl(
  provider: CalendarProvider,
  value: unknown,
): string {
  const raw = text(record(value).authorization_url);
  if (raw.length > 10_000)
    throw new Error(
      "The server returned an invalid calendar authorization address.",
    );
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(
      "The server returned an invalid calendar authorization address.",
    );
  }
  const validProvider =
    provider === "google"
      ? url.hostname === "accounts.google.com" &&
        url.pathname === "/o/oauth2/v2/auth"
      : url.hostname === "login.microsoftonline.com" &&
        /^\/[A-Za-z0-9.-]{1,128}\/oauth2\/v2\.0\/authorize$/.test(url.pathname);
  if (
    !validProvider ||
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.port ||
    url.hash
  )
    throw new Error(
      "Calendar authorization must open on the selected provider's secure sign-in page.",
    );
  for (const key of [
    "client_id",
    "redirect_uri",
    "response_type",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
  ]) {
    if (url.searchParams.getAll(key).length !== 1 || !url.searchParams.get(key))
      throw new Error(
        "Calendar authorization is missing its secure sign-in parameters.",
      );
  }
  if (
    url.searchParams.get("response_type") !== "code" ||
    url.searchParams.get("code_challenge_method") !== "S256" ||
    !/^[A-Za-z0-9_-]{43,128}$/.test(url.searchParams.get("code_challenge")!) ||
    !/^[A-Za-z0-9_-]{20,256}$/.test(url.searchParams.get("state")!)
  )
    throw new Error(
      "Calendar authorization is missing its secure sign-in parameters.",
    );
  return url.href;
}

export function selectionBody(
  connection: CalendarConnection,
  values: readonly string[],
) {
  const selected = [...new Set(values)];
  if (selected.length > 10) throw new Error("Select up to 10 calendars.");
  const allowed = new Set(connection.calendars.map((c) => c.id));
  if (selected.some((id) => !allowed.has(id)))
    throw new Error("Choose calendars available in this connection.");
  return { calendar_ids: selected };
}
export function sameSelection(a: readonly string[], b: readonly string[]) {
  return (
    a.length === b.length &&
    new Set(a).size === a.length &&
    a.every((id) => b.includes(id))
  );
}
export function toggleCalendar(
  connection: CalendarConnection,
  values: readonly string[],
  id: string,
): string[] {
  if (!connection.calendars.some((c) => c.id === id))
    throw new Error("That calendar is no longer available.");
  return selectionBody(
    connection,
    values.includes(id)
      ? values.filter((value) => value !== id)
      : [...values, id],
  ).calendar_ids;
}
export function parseSelectionReceipt(
  value: unknown,
  expected: readonly string[],
) {
  const selected = list(record(value).selected_calendars, identifier);
  if (!sameSelection(selected, expected))
    throw new Error(
      "The server did not confirm your calendar selection. Refresh before retrying.",
    );
  return selected;
}
export function parseCalendarSync(value: unknown) {
  const r = record(value),
    synced_at = text(r.synced_at);
  if (
    typeof r.event_count !== "number" ||
    !Number.isSafeInteger(r.event_count) ||
    r.event_count < 0 ||
    !Number.isFinite(Date.parse(synced_at))
  )
    throw new Error(
      "The server did not confirm calendar synchronization. Refresh before retrying.",
    );
  return { event_count: r.event_count, synced_at };
}
export function parseCalendarDisconnect(value: unknown) {
  const r = record(value);
  if (r.disconnected !== true)
    throw new Error(
      "The server did not confirm the disconnect. Refresh before retrying.",
    );
  return {
    disconnected: true as const,
    provider_revoked: boolean(r.provider_revoked),
    provider_consent_removal_required: boolean(
      r.provider_consent_removal_required,
    ),
  };
}
export function disconnectedCalendar(
  connection: CalendarConnection,
): CalendarConnection {
  return {
    ...connection,
    connected: false,
    status: "disconnected",
    calendars: [],
    selected_calendars: [],
    last_sync_at: null,
    last_error: null,
  };
}
export const calendarStatusLabel = (connection: CalendarConnection) =>
  connection.status === "reauth_required"
    ? "Reconnect needed"
    : connection.status === "error"
      ? "Sync needs attention"
      : connection.connected
        ? "Connected"
        : connection.status === "pending"
          ? "Sign-in pending"
          : connection.available
            ? "Not connected"
            : "Setup needed";

export type CalendarRequest = <T = unknown>(
  path: string,
  body?: unknown,
  method?: string,
) => Promise<T>;
/** Selection is an independent committed write. A failed sync must not undo or
 * misrepresent it, and a malformed selection receipt must never trigger sync. */
export async function syncCalendar(options: {
  request: CalendarRequest;
  connection: CalendarConnection;
  signal: AbortSignal;
  selected?: readonly string[];
  onSelectionSaved: (selected: string[]) => void;
}) {
  let saved = false;
  const path = `/calendars/${options.connection.provider}`;
  options.signal.throwIfAborted();
  if (options.selected !== undefined) {
    const body = selectionBody(options.connection, options.selected);
    const selected = parseSelectionReceipt(
      await options.request(`${path}/selection`, body, "PATCH"),
      body.calendar_ids,
    );
    options.signal.throwIfAborted();
    saved = true;
    options.onSelectionSaved(selected);
  }
  options.signal.throwIfAborted();
  try {
    return parseCalendarSync(await options.request(`${path}/sync`, {}, "POST"));
  } catch (error) {
    throw new Error(
      `${saved ? "Calendar selection saved. " : ""}${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

/** Local removal is authoritative even if the follow-up network read fails. */
export function clearCalendarPreparation(
  data: PreparationData,
  provider: CalendarProvider,
  disconnected = false,
): PreparationData {
  const removed = new Set(
    data.connections
      .filter((c) => c.provider === provider)
      .map((c) => c.connection_id),
  );
  return {
    ...data,
    connections: disconnected
      ? data.connections.filter((c) => c.provider !== provider)
      : data.connections,
    items: data.items.filter((item) => !removed.has(item.meeting.account_id)),
  };
}
