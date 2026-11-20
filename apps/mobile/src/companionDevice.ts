import * as SecureStore from "expo-secure-store";
import { randomUUID } from "expo-crypto";
import {
  companionDeviceLabel,
  createCompanionIdentityReader,
} from "./companionAttachment";

const key = "career-pilot-companion-device-v1";
const options = {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};
const identity = createCompanionIdentityReader(
  {
    read: () => SecureStore.getItemAsync(key, options),
    write: (value) => SecureStore.setItemAsync(key, value, options),
  },
  randomUUID,
);

/** Stable across process recreation; never restored from another device's backup.
 * Used only after an explicit Attach action, not to auto-attach or start capture.
 */
export async function companionDeviceName(platform: string): Promise<string> {
  try {
    return companionDeviceLabel(platform, await identity());
  } catch {
    throw new Error(
      "This phone's secure identity could not be read or saved. Unlock the phone and try connecting again.",
    );
  }
}
