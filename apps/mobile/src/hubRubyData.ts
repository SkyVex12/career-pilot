import { list, nullableText, record, text } from "./workspaceData.ts";
import {
  parseApplication,
  STAGES,
  type Application,
  type Stage,
} from "./applicationData.ts";

export type HubRubyUser = { id: string; name: string };
export type HubRubyConnection = {
  available: boolean;
  connected: boolean;
  required: boolean;
  status: "active" | "revoked" | null;
  user: HubRubyUser | null;
  account_type: "user" | "admin" | "super_admin" | null;
  connected_at: string | null;
  last_pull_at: string | null;
};
export type HubRubyProfile = {
  id: string;
  name: string;
  me: boolean;
  career_os_user_id: string | null;
};
export type HubRubyItem = {
  id: string;
  company: string;
  role: string;
  stage: string;
  user: HubRubyUser | null;
  profile_id: string | null;
  resume: "tailored" | "base" | "none";
  pulled: {
    application_id: string;
    pulled_at: string;
    changed: boolean;
  } | null;
};
export type HubRubyDestination = {
  profileId: string;
  name: string;
  current: boolean;
};
export type HubRubyRequest = <T = unknown>(
  path: string,
  body?: unknown,
  method?: string,
) => Promise<T>;
const invalid = () =>
  new Error(
    "HubRuby returned invalid details. Refresh the connection and try again.",
  );
const bool = (v: unknown) => {
  if (typeof v !== "boolean") throw invalid();
  return v;
};
function nonempty(v: unknown): string {
  const s = text(v);
  if (!s.trim()) throw invalid();
  return s;
}
export function hubRubyId(v: unknown): string {
  const s = text(v);
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(s)) throw invalid();
  return s;
}
function date(v: unknown): string | null {
  if (v == null) return null;
  const s = text(v);
  if (!Number.isFinite(Date.parse(s))) throw invalid();
  return s;
}
function user(v: unknown): HubRubyUser {
  const r = record(v);
  return { id: hubRubyId(r.id), name: text(r.name) };
}
export function parseHubRubyConnection(value: unknown): HubRubyConnection {
  const r = record(value),
    connected = bool(r.connected);
  const status = r.status == null ? null : r.status;
  const kind = r.account_type == null ? null : r.account_type;
  if (status !== null && status !== "active" && status !== "revoked")
    throw invalid();
  if (
    kind !== null &&
    kind !== "user" &&
    kind !== "admin" &&
    kind !== "super_admin"
  )
    throw invalid();
  const person = r.user == null ? null : user(r.user);
  if (connected && (status !== "active" || !person || !kind)) throw invalid();
  return {
    available: bool(r.available),
    connected,
    required: r.required == null ? false : bool(r.required),
    status,
    user: person,
    account_type: kind,
    connected_at: date(r.connected_at),
    last_pull_at: date(r.last_pull_at),
  };
}
export function parseHubRubyProfiles(value: unknown): HubRubyProfile[] {
  const rows = list(record(value).items, (value) => {
    const r = record(value);
    return {
      id: nonempty(r.id),
      name: text(r.name),
      me: bool(r.me),
      career_os_user_id:
        r.career_os_user_id == null ? null : hubRubyId(r.career_os_user_id),
    };
  });
  if (
    rows.filter((p) => p.me).length !== 1 ||
    new Set(rows.map((p) => p.id)).size !== rows.length
  )
    throw invalid();
  return rows;
}
export function parseHubRubyResults(value: unknown) {
  const r = record(value);
  const items = list(r.items, (value) => {
    const i = record(value),
      pulled = i.pulled == null ? null : record(i.pulled);
    if (!["tailored", "base", "none"].includes(String(i.resume)))
      throw invalid();
    const pulledAt = pulled ? date(pulled.pulled_at) : null;
    if (pulled && !pulledAt) throw invalid();
    return {
      id: hubRubyId(i.id),
      company: nonempty(i.company),
      role: nonempty(i.role),
      stage: text(i.stage),
      user: i.user == null ? null : user(i.user),
      profile_id: nullableText(i.profile_id),
      resume: i.resume as HubRubyItem["resume"],
      pulled: pulled
        ? {
            application_id: nonempty(pulled.application_id),
            pulled_at: pulledAt!,
            changed: bool(pulled.changed),
          }
        : null,
    };
  });
  if (new Set(items.map((i) => i.id)).size !== items.length) throw invalid();
  const next = nullableText(r.next_cursor);
  if (next && next.length > 200) throw invalid();
  return { items, next };
}
export function hubRubyCode(value: string) {
  const code = value.trim();
  if (code.length < 6 || code.length > 64)
    throw new Error(
      "Paste the complete one-time code from HubRuby (6–64 characters).",
    );
  return { code };
}
export function hubRubySearchPath(
  query: string,
  stage: Stage | "",
  source: string,
  cursor?: string | null,
) {
  const q = query.trim();
  if (q.length > 200) throw new Error("Search with at most 200 characters.");
  if (stage && !STAGES.some((s) => s.value === stage))
    throw new Error("Choose a valid application stage.");
  if (cursor && cursor.length > 200) throw invalid();
  const params = new URLSearchParams({ limit: "25" });
  if (q) params.set("q", q);
  if (stage) params.set("stage", stage);
  if (source) params.set("user_id", hubRubyId(source));
  if (cursor) params.set("cursor", cursor);
  return `/career-os/applications?${params.toString()}`;
}
/** Never treat an admin's source application as belonging to their own profile. */
export function hubRubyDestination(
  item: HubRubyItem,
  profiles: HubRubyProfile[],
  profile: string,
  connection: HubRubyConnection,
): HubRubyDestination {
  if (!connection.available || !connection.connected)
    throw new Error("Connect HubRuby before importing applications.");
  const current = profiles.find((p) => (profile ? p.id === profile : p.me));
  if (!current)
    throw new Error(
      "Your selected profile is no longer available. Choose a current profile.",
    );
  const sourceId =
    item.user?.id ??
    (connection.account_type === "user" ? connection.user?.id : null);
  if (!sourceId)
    throw new Error(
      "This application's owner is missing. Refresh HubRuby before importing.",
    );
  const destination = profiles.find((p) => p.career_os_user_id === sourceId);
  if (!destination || (item.profile_id && item.profile_id !== destination.id))
    throw new Error(
      "This application's owner is no longer available to this account. Refresh HubRuby and profiles.",
    );
  if (!current.me && destination.id !== current.id)
    throw new Error(
      "This application belongs to another profile. Switch to its owner before importing.",
    );
  if (connection.account_type === "user" && sourceId !== connection.user?.id)
    throw new Error(
      "This application does not belong to the linked HubRuby account.",
    );
  return {
    profileId: destination.id,
    name: destination.name || item.user?.name || sourceId,
    current: destination.id === current.id,
  };
}
/** Validate the destination both before writing and after the server's owner-bound pull. */
export async function pullHubRubyApplication(options: {
  request: HubRubyRequest;
  item: HubRubyItem;
  profile: string;
  expected: HubRubyDestination;
  signal: AbortSignal;
}): Promise<{
  application: Application;
  destination: HubRubyDestination;
  again: boolean;
}> {
  const { request, item, signal } = options;
  signal.throwIfAborted();
  const connection = parseHubRubyConnection(
    await request("/career-os/connection"),
  );
  signal.throwIfAborted();
  const profiles = parseHubRubyProfiles(await request("/profiles"));
  signal.throwIfAborted();
  const destination = hubRubyDestination(
    item,
    profiles,
    options.profile,
    connection,
  );
  if (
    destination.profileId !== options.expected.profileId ||
    destination.current !== options.expected.current
  )
    throw new Error(
      "The destination profile changed. Search again before importing.",
    );
  const raw = record(
    await request(
      `/career-os/applications/${encodeURIComponent(hubRubyId(item.id))}/pull`,
      undefined,
      "POST",
    ),
  );
  signal.throwIfAborted();
  const application = parseApplication(raw, true);
  if (
    raw.profile_id !== destination.profileId ||
    application.career_os?.application_id !== item.id ||
    (item.pulled && application.id !== item.pulled.application_id) ||
    !["new", "again"].includes(String(raw.pulled))
  )
    throw new Error(
      "The import response did not match this application and profile. Refresh before continuing.",
    );
  return { application, destination, again: raw.pulled === "again" };
}
export function parseHubRubyStatus(value: unknown) {
  const r = record(value),
    pulled = date(r.pulled_at);
  if (!pulled) throw invalid();
  return {
    changed: bool(r.changed),
    pulled_at: pulled,
    updated_at: date(r.updated_at),
  };
}
export function hubRubyError(value: string) {
  return value.includes("only_sign_in_method")
    ? "HubRuby is your account's only sign-in method. Add another sign-in in desktop Settings → Account before disconnecting."
    : value;
}
