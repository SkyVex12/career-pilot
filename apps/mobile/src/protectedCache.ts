import {
  matchesPrefix,
  type ResponsePersistence,
  type StoredResponse,
} from "./dataCache.ts";

type RecordEntry = {
  namespace: string;
  path: string;
  value: StoredResponse;
  bytes: number;
};
/** Storage sees ciphertext only; native keys never enter the serialized archive. */
export interface ProtectedCacheIO {
  read(): Promise<Uint8Array | null>;
  write(ciphertext: Uint8Array): Promise<void>;
  clear(): Promise<void>;
  encrypt(plaintext: Uint8Array): Promise<Uint8Array>;
  decrypt(ciphertext: Uint8Array): Promise<Uint8Array>;
}
export class ProtectedResponseCache implements ResponsePersistence {
  private queue: Promise<unknown> = Promise.resolve();
  private records: RecordEntry[] | null = null;
  private io: ProtectedCacheIO;
  private budget: number;
  constructor(io: ProtectedCacheIO, budget = 2 * 1024 * 1024) {
    this.io = io;
    this.budget = budget;
  }
  private serial<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.queue.catch(() => {}).then(operation);
    this.queue = result;
    return result;
  }
  private async load(): Promise<RecordEntry[]> {
    if (this.records) return this.records;
    try {
      const encrypted = await this.io.read();
      if (!encrypted) return (this.records = []);
      if (encrypted.byteLength > this.budget + 4096) {
        await this.io.clear();
        return (this.records = []);
      }
      const raw = JSON.parse(
        new TextDecoder().decode(await this.io.decrypt(encrypted)),
      ) as RecordEntry[];
      if (!Array.isArray(raw)) throw new Error("Invalid cache archive.");
      this.records = raw
        .filter(
          (entry) =>
            typeof entry.namespace === "string" &&
            typeof entry.path === "string" &&
            Number.isFinite(entry.bytes) &&
            entry.bytes >= 0 &&
            entry.value &&
            Number.isFinite(entry.value.savedAt) &&
            Date.now() - entry.value.savedAt < 30 * 86_400_000,
        )
        .slice(-64);
    } catch {
      this.records = [];
      await this.io.clear().catch(() => {});
    }
    return this.records;
  }
  private async save(): Promise<void> {
    await this.io.write(
      await this.io.encrypt(
        new TextEncoder().encode(JSON.stringify(this.records ?? [])),
      ),
    );
  }
  read(namespace: string, path: string): Promise<StoredResponse | null> {
    return this.serial(async () => {
      const records = await this.load(),
        index = records.findIndex(
          (entry) => entry.namespace === namespace && entry.path === path,
        );
      if (index < 0) return null;
      const [entry] = records.splice(index, 1);
      records.push(entry);
      return JSON.parse(JSON.stringify(entry.value)) as StoredResponse;
    });
  }
  write(namespace: string, path: string, value: StoredResponse): Promise<void> {
    return this.serial(async () => {
      const records = (await this.load()).filter(
        (entry) => entry.namespace !== namespace || entry.path !== path,
      );
      const entry = { namespace, path, value, bytes: 0 };
      entry.bytes =
        new TextEncoder().encode(JSON.stringify(entry)).byteLength + 32;
      // Large transcripts remain eligible for memory caching without exhausting local disk.
      if (entry.bytes > Math.min(this.budget / 2, 512 * 1024)) return;
      records.push(entry);
      let size = records.reduce((sum, item) => sum + item.bytes, 2);
      while (size > this.budget || records.length > 64)
        size -= records.shift()!.bytes;
      this.records = records;
      await this.save();
    });
  }
  remove(namespace: string, path: string): Promise<void> {
    return this.serial(async () => {
      this.records = (await this.load()).filter(
        (entry) => entry.namespace !== namespace || entry.path !== path,
      );
      await this.save();
    });
  }
  invalidate(namespace: string, prefixes: string[]): Promise<void> {
    return this.serial(async () => {
      const old = await this.load();
      this.records = old.filter(
        (entry) =>
          entry.namespace !== namespace ||
          !prefixes.some((prefix) => matchesPrefix(entry.path, prefix)),
      );
      if (this.records.length !== old.length) await this.save();
    });
  }
  clear(): Promise<void> {
    return this.serial(async () => {
      this.records = [];
      await this.io.clear();
    });
  }
}
