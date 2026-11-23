import type { Choice } from "./choiceState.ts";

const languageId = /^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;
const incompatible = () =>
  new Error(
    "The server returned invalid language settings. Retry or update the server.",
  );

function label(value: unknown): value is string {
  return typeof value === "string" && !!value.trim() && value.length <= 100;
}

/** Only server-advertised languages are selectable; never synthesize an English fallback. */
export function parseCallLanguages(value: unknown): Choice[] {
  if (!value || typeof value !== "object" || !("items" in value))
    throw incompatible();
  const { items } = value;
  if (!Array.isArray(items) || items.length > 1000) throw incompatible();
  const seen = new Set<string>();
  return items.map((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.id !== "string" ||
      !languageId.test(item.id) ||
      item.id.length > 100 ||
      seen.has(item.id) ||
      !label(item.name) ||
      (item.native_name !== undefined && !label(item.native_name)) ||
      (item.direction !== undefined &&
        item.direction !== "ltr" &&
        item.direction !== "rtl")
    )
      throw incompatible();
    seen.add(item.id);
    return {
      value: item.id,
      label: item.name,
      ...(item.native_name && item.native_name !== item.name
        ? {
            nativeLabel: item.native_name,
            nativeDirection: item.direction ?? "ltr",
          }
        : {}),
    };
  });
}

export async function loadCallLanguages(
  request: (path: string) => Promise<unknown>,
) {
  const [catalog, preference] = await Promise.allSettled([
    request("/call-languages").then(parseCallLanguages),
    request("/profile/call-preferences").then((value) => {
      if (
        !value ||
        typeof value !== "object" ||
        !("spoken_language" in value) ||
        typeof value.spoken_language !== "string" ||
        value.spoken_language.length > 100 ||
        !languageId.test(value.spoken_language)
      )
        throw incompatible();
      return value.spoken_language;
    }),
  ]);
  if (catalog.status === "rejected") throw catalog.reason;
  if (!catalog.value.length)
    return {
      choices: catalog.value,
      selected: "",
      warning:
        "No spoken languages are enabled on this server. Check its speech provider configuration, then retry.",
    };
  if (preference.status === "rejected")
    return {
      choices: catalog.value,
      selected: "",
      warning:
        "Could not load this profile’s language. Choose a spoken language before starting.",
    };
  const available = catalog.value.some(
    (choice) => choice.value === preference.value,
  );
  return {
    choices: catalog.value,
    selected: available ? preference.value : "",
    warning: available
      ? ""
      : "This profile’s saved language is unavailable on this server. Choose a supported spoken language.",
  };
}
