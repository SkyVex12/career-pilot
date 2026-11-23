export const qualities = ["720p", "1080p", "1440p", "2160p"] as const;
export type ScreenQuality = (typeof qualities)[number];
export type RemoteInput =
  | { t: "move"; x: number; y: number }
  | { t: "down" | "up"; button: number; x: number; y: number }
  | { t: "wheel"; dx: number; dy: number }
  | { t: "key_down" | "key_up"; code: string };
export type ScreenConsent = { requestId: string | null; controlling: boolean };
export function controlReply(
  state: ScreenConsent,
  sender: string | undefined,
  value: unknown,
): ScreenConsent {
  if (sender !== "host" || !value || typeof value !== "object") return state;
  const message = value as { on?: unknown; requestId?: unknown };
  if (message.on === false) return { requestId: null, controlling: false };
  if (
    message.on === true &&
    state.requestId !== null &&
    message.requestId === state.requestId
  )
    return { requestId: null, controlling: true };
  return state;
}
export function screenPoint(
  x: number,
  y: number,
  width: number,
  height: number,
): { x: number; y: number } | null {
  if (
    ![x, y, width, height].every(Number.isFinite) ||
    width <= 0 ||
    height <= 0 ||
    x < 0 ||
    y < 0 ||
    x > width ||
    y > height
  )
    return null;
  return { x: x / width, y: y / height };
}
export function keyStroke(code: string): RemoteInput[] {
  return [
    { t: "key_down", code },
    { t: "key_up", code },
  ];
}
/** Remote injection accepts physical key codes. Do not silently corrupt non-US text. */
export function textKeys(text: string): RemoteInput[] {
  if (text.length > 500)
    throw new Error("Send at most 500 characters at once.");
  const normal = "`1234567890-=[]\\;',./",
    shifted = '~!@#$%^&*()_+{}|:"<>?';
  const codes = [
    "Backquote",
    ..."1234567890".split("").map((x) => `Digit${x}`),
    "Minus",
    "Equal",
    "BracketLeft",
    "BracketRight",
    "Backslash",
    "Semicolon",
    "Quote",
    "Comma",
    "Period",
    "Slash",
  ];
  const events: RemoteInput[] = [];
  for (const character of text) {
    let code = "",
      shift = false;
    if (/^[a-z]$/i.test(character)) {
      code = `Key${character.toUpperCase()}`;
      shift = character !== character.toLowerCase();
    } else if (character === " ") code = "Space";
    else if (character === "\n") code = "Enter";
    else if (character === "\t") code = "Tab";
    else if (normal.includes(character))
      code = codes[normal.indexOf(character)];
    else if (shifted.includes(character)) {
      code = codes[shifted.indexOf(character)];
      shift = true;
    } else
      throw new Error(
        "Remote typing supports a US English keyboard. Use the host keyboard for other scripts.",
      );
    if (shift) events.push({ t: "key_down", code: "ShiftLeft" });
    events.push(...keyStroke(code));
    if (shift) events.push({ t: "key_up", code: "ShiftLeft" });
  }
  return events;
}
