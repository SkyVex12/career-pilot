import { ApiError, type Api } from "./api.ts";

/** A phone's cached roster must not restore access revoked on another device. */
export async function updateAssistantPermissions(
  api: Pick<Api, "request">,
  sessionId: string,
  seatId: string,
  previous: readonly string[],
  next: readonly string[],
  onConflict: () => void,
): Promise<unknown> {
  try {
    return await api.request(
      `/sessions/${encodeURIComponent(sessionId)}/seats/${encodeURIComponent(seatId)}`,
      { capabilities: [...next], expected_capabilities: [...previous] },
      "PATCH",
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) onConflict();
    // Never retry a conflict with newly read permissions: the host must review them.
    throw error;
  }
}
