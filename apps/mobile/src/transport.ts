/** Expo replaces this literal environment lookup in the embedded JavaScript bundle. */
export const HTTP_TEST_BUILD =
  process.env.EXPO_PUBLIC_MOBILE_HTTP_TEST === "true";
export const DEVELOPMENT_BUILD = typeof __DEV__ !== "undefined" && __DEV__;

/** Shared REST and WebSocket policy. Native release policy also rejects cleartext. */
export function transportURL(
  raw: string,
  kind: "rest" | "websocket",
  development = false,
  httpTest = HTTP_TEST_BUILD,
): URL {
  const u = new URL(raw);
  // URL.search/hash omit empty delimiters; reject those too. Backslashes are
  // normalized by URL parsers and can obscure the intended authority.
  const authority = raw.trim().match(/^[a-z][a-z\d+.-]*:\/\/([^/\\?#]*)/i)?.[1];
  if (
    u.username ||
    u.password ||
    raw.includes("?") ||
    raw.includes("#") ||
    raw.includes("\\") ||
    authority?.includes("@")
  )
    throw new Error(
      "Server address must not contain credentials, queries or fragments.",
    );
  const secure = kind === "rest" ? "https:" : "wss:";
  const cleartext = kind === "rest" ? "http:" : "ws:";
  const local = ["localhost", "127.0.0.1", "10.0.2.2"].includes(u.hostname);
  if (
    u.protocol !== secure &&
    !(u.protocol === cleartext && (httpTest || (development && local)))
  )
    throw new Error(
      kind === "rest"
        ? "Use an HTTPS server address."
        : "Use a secure WebSocket address.",
    );
  return u;
}
