import * as SecureStore from "expo-secure-store";
import { digestStringAsync, CryptoDigestAlgorithm } from "expo-crypto";
import type { SecretStore, Tokens } from "./api";
const options = {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};
export async function secretsFor(server: string): Promise<SecretStore> {
  const prefix = `careerpilot.${(await digestStringAsync(CryptoDigestAlgorithm.SHA256, server)).slice(0, 32)}`;
  return {
    async read() {
      const [access, refresh, meta] = await Promise.all(
        ["access", "refresh", "meta"].map((k) =>
          SecureStore.getItemAsync(`${prefix}.${k}`, options),
        ),
      );
      return access && refresh && meta
        ? ({
            ...JSON.parse(meta),
            access_token: access,
            refresh_token: refresh,
          } as Tokens)
        : null;
    },
    async write(tokens) {
      if (!tokens) {
        await Promise.all(
          ["access", "refresh", "meta"].map((k) =>
            SecureStore.deleteItemAsync(`${prefix}.${k}`, options),
          ),
        );
        return;
      }
      await SecureStore.setItemAsync(
        `${prefix}.access`,
        tokens.access_token,
        options,
      );
      await SecureStore.setItemAsync(
        `${prefix}.refresh`,
        tokens.refresh_token,
        options,
      );
      await SecureStore.setItemAsync(
        `${prefix}.meta`,
        JSON.stringify({
          email: tokens.email,
          expires_in: tokens.expires_in,
          expires_at: tokens.expires_at,
        }),
        options,
      );
    },
  };
}
