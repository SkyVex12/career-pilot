import { rest, type HostSeat, type Settings } from "./api";

export interface GuestPerson { email: string; name?: string }
export interface PlannedGuest extends GuestPerson { screen: boolean; control: boolean }
export interface InviteFailure { guest: PlannedGuest; error: string }
export interface InviteProgress { sessionId: string; scope: string; pending: number; failures: InviteFailure[] }
const RECENT = "ruby.recentGuests.v1:";
const PROGRESS = "ruby.guestInvitations.v1";
export const GUESTS_CHANGED = "ruby-guests-changed";
export const validGuestEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
export const normalizeGuestEmail = (email: string) => email.trim().toLowerCase();

/** Account and profile isolation applies to contacts as well as call context. */
export function guestScope(settings: Settings | null, account: string | null, environment: string = settings?.environment ?? "local"): string {
  return JSON.stringify([environment, environment === "cloud" ? settings?.cloud_api_url : settings?.api_url, account?.toLowerCase() ?? "local", settings?.profile ?? null]);
}
function read(key: string): unknown {
  try { return JSON.parse(localStorage.getItem(key) ?? "null"); } catch { return null; }
}
function write(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Contact history is optional. */ }
  window.dispatchEvent(new Event(GUESTS_CHANGED));
}
export function recentGuests(scope: string): GuestPerson[] {
  const value = read(RECENT + scope);
  return Array.isArray(value) ? value.filter((p): p is GuestPerson => p && typeof p.email === "string" && validGuestEmail(p.email) && (p.name === undefined || typeof p.name === "string")).slice(0, 20) : [];
}
export function rememberGuest(scope: string, person: GuestPerson) {
  const email = normalizeGuestEmail(person.email);
  if (!validGuestEmail(email)) return;
  const existing = recentGuests(scope);
  const old = existing.find(p => p.email === email);
  const name = person.name?.trim() || old?.name;
  write(RECENT + scope, [{ email, ...(name && name.toLowerCase() !== email ? { name } : {}) }, ...existing.filter(p => p.email !== email)].slice(0, 20));
}
/** Names are learned from authenticated seat records, never inferred from an email. */
export function learnGuestNames(scope: string, seats: HostSeat[]) {
  const contacts = recentGuests(scope);
  for (const seat of seats) if (contacts.some(p => p.email === normalizeGuestEmail(seat.email)) && seat.name) rememberGuest(scope, { email: seat.email, name: seat.name });
}
export function inviteProgress(sessionId: string): InviteProgress | null {
  const value = read(PROGRESS) as InviteProgress | null;
  return value?.sessionId === sessionId && typeof value.scope === "string" && Array.isArray(value.failures) ? value : null;
}
export function guestCapabilities(guest: PlannedGuest, media: { voice?: boolean; screen?: boolean }): string[] {
  return ["note", ...(media.voice ? ["voice"] : []), ...(media.screen && (guest.screen || guest.control) ? ["device.view"] : []), ...(media.screen && guest.control ? ["device.control"] : [])];
}
/** Called only after the native start command succeeds and its new session ID is known. */
export async function invitePlannedGuests(sessionId: string, scope: string, guests: PlannedGuest[]) {
  const selected = guests.slice(0, 2).map(g => ({ ...g, email: normalizeGuestEmail(g.email) }));
  if (!selected.length) return;
  const progress: InviteProgress = { sessionId, scope, pending: selected.length, failures: [] };
  write(PROGRESS, progress);
  let media: Awaited<ReturnType<typeof rest.seatsOf>>;
  try { media = await rest.seatsOf(sessionId); }
  catch (error) {
    write(PROGRESS, { ...progress, pending: 0, failures: selected.map(guest => ({ guest, error: String(error) })) });
    return;
  }
  for (const guest of selected) {
    try {
      await rest.inviteGuest(sessionId, guest.email, guestCapabilities(guest, media));
      rememberGuest(scope, guest);
    } catch (error) { progress.failures.push({ guest, error: String(error) }); }
    progress.pending--;
    write(PROGRESS, progress);
  }
  try { learnGuestNames(scope, (await rest.seatsOf(sessionId)).seats); } catch { /* The invitation already succeeded. */ }
}
