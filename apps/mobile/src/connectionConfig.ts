import { secureBase } from "./api.ts";
import { gatewayURL } from "./liveState.ts";
import { HTTP_TEST_BUILD } from "./transport.ts";

function automaticGateway(api: string): string {
  return `${secureBase(api, true, true).replace(/^http/, "ws")}/v1/companion`;
}

/** Saved connections contain a resolved socket URL. Recover only a real override
 * so editing the workspace address never retains the old automatic endpoint. */
export function gatewayOverride(api: string, gateway: string): string {
  const value = gateway.trim();
  if (!value) return "";
  try {
    return gatewayURL(value, true, true) === automaticGateway(api) ? "" : value;
  } catch {
    // Keep invalid custom input visible for correction; connection validates it.
    return value;
  }
}

export function resolveConnection(
  api: string,
  override: string,
  development = false,
  httpTest = HTTP_TEST_BUILD,
): { api: string; gateway: string } {
  const base = secureBase(api, development, httpTest);
  return {
    api: base,
    gateway: gatewayURL(
      override.trim() || automaticGateway(base),
      development,
      httpTest,
    ),
  };
}
