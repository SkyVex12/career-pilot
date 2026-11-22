import type { Api } from "./api";
export type CareerOsOption = { authorize_url: string; client_id: string };
export type AuthConfig = {
  provider: "google" | "cognito" | "dev";
  email: {
    code_length: number;
    code_ttl_s: number;
    resend_after_s: number;
  } | null;
  career_os: CareerOsOption | null;
};
export function parseAuthConfig(raw: unknown): AuthConfig {
  if (!raw || typeof raw !== "object")
    throw new Error("Invalid server sign-in configuration.");
  const data = raw as Record<string, unknown>;
  if (!["google", "cognito", "dev"].includes(String(data.provider)))
    throw new Error("Unsupported server sign-in provider.");
  let email: AuthConfig["email"] = null;
  if (data.email != null) {
    const e = data.email as Record<string, unknown>;
    if (
      ![e.code_length, e.code_ttl_s, e.resend_after_s].every(
        (v) => typeof v === "number" && Number.isInteger(v) && v > 0,
      )
    )
      throw new Error("Invalid email sign-in configuration.");
    email = {
      code_length: e.code_length as number,
      code_ttl_s: e.code_ttl_s as number,
      resend_after_s: e.resend_after_s as number,
    };
  }
  let career_os: CareerOsOption | null = null;
  if (data.career_os != null) {
    const c = data.career_os as Record<string, unknown>;
    if (
      typeof c.authorize_url !== "string" ||
      typeof c.client_id !== "string" ||
      !c.client_id
    )
      throw new Error("Invalid HubRuby sign-in configuration.");
    const url = new URL(c.authorize_url);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      throw new Error("HubRuby sign-in requires a secure browser address.");
    career_os = { authorize_url: url.toString(), client_id: c.client_id };
  }
  return {
    provider: data.provider as AuthConfig["provider"],
    email,
    career_os,
  };
}
export async function loadAuthConfig(
  api: Api,
  signal?: AbortSignal,
): Promise<AuthConfig> {
  return parseAuthConfig(
    await api.request("/auth/config", undefined, "GET", false, signal),
  );
}
