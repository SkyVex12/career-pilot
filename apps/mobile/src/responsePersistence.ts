import { File, Paths } from "expo-file-system";
import * as SecureStore from "expo-secure-store";
import {
  AESEncryptionKey,
  AESSealedData,
  aesEncryptAsync,
  aesDecryptAsync,
  digestStringAsync,
  CryptoDigestAlgorithm,
} from "expo-crypto";
import { ProtectedResponseCache } from "./protectedCache";

/** Disposable ciphertext lives in the OS cache directory; its device-only key lives in Keychain/Keystore. */
export async function responsePersistence(
  server: string,
): Promise<ProtectedResponseCache> {
  const digest = (
    await digestStringAsync(CryptoDigestAlgorithm.SHA256, server)
  ).slice(0, 32);
  const keyName = `careerpilot.${digest}.response-cache-key`;
  const file = new File(Paths.cache, `careerpilot-responses-${digest}.bin`);
  const options = {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  };
  let encryptionKey: AESEncryptionKey | null = null;
  async function key(): Promise<AESEncryptionKey> {
    if (encryptionKey) return encryptionKey;
    const encoded = await SecureStore.getItemAsync(keyName, options);
    if (encoded)
      return (encryptionKey = await AESEncryptionKey.import(encoded, "base64"));
    const generated = await AESEncryptionKey.generate();
    await SecureStore.setItemAsync(
      keyName,
      await generated.encoded("base64"),
      options,
    );
    return (encryptionKey = generated);
  }
  return new ProtectedResponseCache({
    read: async () => (file.exists ? file.bytes() : null),
    write: async (bytes) => {
      const temporary = new File(
        Paths.cache,
        `careerpilot-responses-${digest}.tmp`,
      );
      temporary.create({ overwrite: true });
      temporary.write(bytes);
      temporary.move(file, { overwrite: true });
    },
    clear: async () => {
      const temporary = new File(
        Paths.cache,
        `careerpilot-responses-${digest}.tmp`,
      );
      if (temporary.exists) temporary.delete();
      if (file.exists) file.delete();
      encryptionKey = null;
      await SecureStore.deleteItemAsync(keyName, options);
    },
    encrypt: async (bytes) =>
      (await aesEncryptAsync(bytes, await key())).combined(),
    decrypt: async (bytes) =>
      aesDecryptAsync(AESSealedData.fromCombined(bytes), await key()),
  });
}
