// The cloud environment's URLs (src-tauri settings.rs: Settings.cloud_*).

/** Both cloud URLs from the stage's host or IP (terraform output external_ip), or from a URL. */
export function cloudUrlsFor(hostOrUrl: string): { cloud_api_url: string; cloud_gateway_url: string } | null {
  const raw = hostOrUrl.trim();
  if (!raw) return null;
  const secure = /^(https|wss):\/\//.test(raw);
  const host = raw.replace(/^[a-z]+:\/\//, "").split("/")[0];
  if (!host) return null;
  return {
    cloud_api_url: `${secure ? "https" : "http"}://${host}/v1`,
    cloud_gateway_url: `${secure ? "wss" : "ws"}://${host}/v1/session`,
  };
}
