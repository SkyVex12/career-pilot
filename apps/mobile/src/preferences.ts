import * as SecureStore from "expo-secure-store";
export type ConnectionPreference = { api: string; gateway: string };
const key = "career-pilot-connection-v1";
export function parseConnectionPreference(
  raw: string | null,
): ConnectionPreference | null {
  if (!raw || raw.length > 4096) return null;
  try {
    const value = JSON.parse(raw);
    return typeof value?.api === "string" && typeof value.gateway === "string"
      ? { api: value.api, gateway: value.gateway }
      : null;
  } catch {
    return null;
  }
}
export async function readConnectionPreference() {
  return parseConnectionPreference(
    await SecureStore.getItemAsync(key).catch(() => null),
  );
}
export async function saveConnectionPreference(value: ConnectionPreference) {
  await SecureStore.setItemAsync(key, JSON.stringify(value)).catch(() => {});
}
