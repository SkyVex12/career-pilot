/** Pure, bounded matching for this phone's existing companion display grant.
 * The list must come from a fresh authenticated request for the current account.
 * Account ownership is enforced by the endpoint; its rows contain no account ID.
 */
const deviceUuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const recordUuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export interface CompanionIdentityStore {
  read(): Promise<string | null>;
  write(value: string): Promise<void>;
}
/** Single-flight identity creation prevents simultaneous callers from persisting different IDs.
 * A failed read/write never returns an ephemeral identity that could leak another attachment.
 */
export function createCompanionIdentityReader(
  store: CompanionIdentityStore,
  createUuid: () => string,
): () => Promise<string> {
  let pending: Promise<string> | null = null;
  return () => {
    if (!pending) {
      pending = (async () => {
        const previous = await store.read();
        if (typeof previous === "string" && deviceUuid.test(previous))
          return previous.toLowerCase();
        const created = createUuid();
        if (!deviceUuid.test(created))
          throw new Error(
            "Could not create a secure phone identity. Try again.",
          );
        const id = created.toLowerCase();
        await store.write(id);
        return id;
      })().catch((error) => {
        pending = null;
        throw error;
      });
    }
    return pending;
  };
}
export function companionDeviceLabel(platform: string, id: string): string {
  if (!deviceUuid.test(id))
    throw new Error("This phone's secure identity is invalid. Try again.");
  const name =
    platform === "android"
      ? "Android phone"
      : platform === "ios"
        ? "iOS phone"
        : "Phone";
  return `${name} · ${id.toLowerCase()}`;
}
export type ReusableCompanionAttachment = {
  attachment_id: string;
  profile_id: string;
  session_id: string;
  device_name: string;
  expires_at: string;
  revoked: false;
};
export type CompanionAttachmentMatch = {
  deviceName: string;
  profileId: string;
  sessionId: string;
  excludedIds?: ReadonlySet<string>;
};
function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function expiresAt(value: unknown): number | null {
  if (typeof value !== "string" || value.length > 40) return null;
  const parts =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(
      value,
    );
  if (!parts) return null;
  const [
    ,
    year,
    month,
    day,
    hour,
    minute,
    second,
    offsetHour = "0",
    offsetMinute = "0",
  ] = parts;
  const calendar = new Date(Date.UTC(+year, +month - 1, +day));
  if (
    calendar.getUTCFullYear() !== +year ||
    calendar.getUTCMonth() !== +month - 1 ||
    calendar.getUTCDate() !== +day ||
    +hour > 23 ||
    +minute > 59 ||
    +second > 59 ||
    +offsetHour > 23 ||
    +offsetMinute > 59
  )
    return null;
  const expiry = Date.parse(value);
  return Number.isFinite(expiry) ? expiry : null;
}
/** Reuse only an exact device/profile/session match. Never guesses from the label prefix.
 * Expired, revoked, malformed, and duplicate records cannot grant a reusable display.
 */
export function findReusableCompanionAttachment(
  value: unknown,
  expected: CompanionAttachmentMatch,
  nowMs = Date.now(),
): ReusableCompanionAttachment | null {
  const identity = /^(?:Android phone|iOS phone|Phone) · (.+)$/.exec(
    expected.deviceName,
  );
  if (
    !identity ||
    !deviceUuid.test(identity[1]) ||
    !recordUuid.test(expected.profileId) ||
    !recordUuid.test(expected.sessionId) ||
    !Number.isFinite(nowMs)
  )
    throw new Error(
      "The current phone, profile or session identity is unavailable. Refresh the session list.",
    );
  if (!object(value) || !Array.isArray(value.items) || value.items.length > 100)
    throw new Error(
      "The server returned an incompatible companion attachment list. Update the server or retry the connection.",
    );
  const counts = new Map<string, number>();
  for (const row of value.items)
    if (object(row) && typeof row.attachment_id === "string")
      counts.set(row.attachment_id, (counts.get(row.attachment_id) ?? 0) + 1);
  let newest: { item: ReusableCompanionAttachment; expiry: number } | null =
    null;
  for (const row of value.items) {
    if (
      !object(row) ||
      typeof row.attachment_id !== "string" ||
      !recordUuid.test(row.attachment_id) ||
      expected.excludedIds?.has(row.attachment_id) ||
      counts.get(row.attachment_id) !== 1 ||
      row.revoked !== false ||
      row.device_name !== expected.deviceName ||
      row.profile_id !== expected.profileId ||
      row.session_id !== expected.sessionId
    )
      continue;
    const expiry = expiresAt(row.expires_at);
    if (
      expiry === null ||
      expiry <= nowMs ||
      (newest && newest.expiry >= expiry)
    )
      continue;
    newest = {
      expiry,
      item: {
        attachment_id: row.attachment_id,
        profile_id: expected.profileId,
        session_id: expected.sessionId,
        device_name: expected.deviceName,
        expires_at: row.expires_at as string,
        revoked: false,
      },
    };
  }
  return newest?.item ?? null;
}
