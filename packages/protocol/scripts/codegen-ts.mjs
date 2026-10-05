// Generates src/generated.ts from protocol.schema.json.
// Usage: node scripts/codegen-ts.mjs [output-path]   (run via `pnpm codegen` at the repo root)
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = resolve(process.argv[2] ?? resolve(root, "src/generated.ts"));
const schema = JSON.parse(await readFile(resolve(root, "protocol.schema.json"), "utf8"));

const options = {
  bannerComment: "",
  // The schema leaves extra fields allowed so older peers tolerate newer ones;
  // the TypeScript types still list only the known fields.
  additionalProperties: false,
  strictIndexSignatures: true,
  format: true,
  style: { singleQuote: false, semi: true, trailingComma: "all", printWidth: 100 },
};

const parts = [
  "/* Generated from protocol.schema.json by json-schema-to-typescript. Do not edit; run `pnpm codegen`. */\n",
  await compile(schema, "Message", options),
];

// Definitions not reachable from the root Message (AudioFrameHeader, CloseCode) are not
// emitted by the pass above (`unreachableDefinitions` ignores `$defs`), so compile each one.
const declared = (text) => new Set([...text.matchAll(/export (?:type|interface) (\w+)/g)].map((m) => m[1]));
for (const name of Object.keys(schema.$defs)) {
  if (declared(parts.join("\n")).has(name)) continue;
  const def = { ...schema.$defs[name], title: name, $defs: schema.$defs };
  parts.push(await compile(def, name, { ...options, declareExternallyReferenced: false }));
}

await writeFile(out, parts.join("\n").replace(/\r\n/g, "\n"));
