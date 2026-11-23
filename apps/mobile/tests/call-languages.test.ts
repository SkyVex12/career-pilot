import test from "node:test";
import assert from "node:assert/strict";
import { parseCallLanguages, loadCallLanguages } from "../src/callLanguages.ts";
import { filterChoices } from "../src/choiceState.ts";
import { parsePracticeChoices } from "../src/practiceState.ts";

const catalog = {
  items: [
    { id: "en", name: "English", native_name: "English", direction: "ltr" },
    {
      id: "vi",
      name: "Vietnamese",
      native_name: "Tiếng Việt",
      direction: "ltr",
    },
    { id: "ar", name: "Arabic", native_name: "العربية", direction: "rtl" },
    {
      id: "zh-Hant",
      name: "Chinese (Traditional)",
      native_name: "繁體中文",
      direction: "ltr",
    },
  ],
};

test("phone call and practice selectors preserve native names and text direction", () => {
  const choices = parseCallLanguages(catalog);
  assert.deepEqual(choices[0], { value: "en", label: "English" });
  assert.deepEqual(choices[2], {
    value: "ar",
    label: "Arabic",
    nativeLabel: "العربية",
    nativeDirection: "rtl",
  });
  assert.deepEqual(parsePracticeChoices(catalog, "languages"), choices);
  for (const query of ["vietnamese", "  TIENG VIET  ", "vi"]) {
    assert.equal(filterChoices(choices, query)[0]?.value, "vi");
  }
  assert.equal(filterChoices(choices, "العربية")[0]?.value, "ar");
  assert.equal(filterChoices(choices, "繁體")[0]?.value, "zh-Hant");
  assert.equal(filterChoices(choices, "zh-Hant")[0]?.value, "zh-Hant");
  assert.equal(
    filterChoices(choices, "traditional chinese")[0]?.value,
    "zh-Hant",
  );
  assert.deepEqual(filterChoices(choices, "missing language"), []);
  assert.equal(filterChoices(choices, "  "), choices);
});

test("a catalog larger than 100 entries remains complete and searchable", () => {
  // Synthetic identifiers exercise the UI boundary, not provider eligibility.
  const items = Array.from({ length: 150 }, (_, index) => ({
    id: `q${String.fromCharCode(97 + Math.floor(index / 26))}${String.fromCharCode(97 + (index % 26))}`,
    name: `Catalog language ${index}`,
    native_name: `Native label ${index}`,
  }));
  const choices = parseCallLanguages({ items });
  assert.equal(choices.length, 150);
  assert.equal(filterChoices(choices, "native 149")[0]?.value, items[149].id);
  assert.deepEqual(parsePracticeChoices({ items }, "languages"), choices);
});

test("malformed or duplicate advertised languages do not become selectable", () => {
  for (const value of [
    null,
    {},
    { items: "en" },
    { items: [{ id: "English", name: "English" }] },
    { items: [{ id: "en", name: " " }] },
    { items: [{ id: "en", name: "English", native_name: {} }] },
    { items: [{ id: "ar", name: "Arabic", direction: "sideways" }] },
    { items: [catalog.items[0], catalog.items[0]] },
    { items: Array(1001).fill(catalog.items[0]) },
  ])
    assert.throws(() => parseCallLanguages(value), /invalid language settings/);
  assert.deepEqual(parseCallLanguages({ items: [] }), []);
});

test("in-person setup selects the profile language and never silently substitutes English", async () => {
  const load = (spoken_language: string) =>
    loadCallLanguages(async (path) =>
      path === "/call-languages" ? catalog : { spoken_language },
    );
  const saved = await load("ar");
  assert.equal(saved.selected, "ar");
  assert.equal(saved.warning, "");
  const unavailable = await load("fr");
  assert.equal(unavailable.selected, "");
  assert.match(unavailable.warning, /saved language is unavailable/);
  assert.equal(unavailable.choices.length, catalog.items.length);
  const empty = await loadCallLanguages(async (path) =>
    path === "/call-languages" ? { items: [] } : { spoken_language: "en" },
  );
  assert.equal(empty.selected, "");
  assert.match(empty.warning, /No spoken languages are enabled/);
});

test("failed preferences allow an explicit choice but unavailable catalogs never fabricate options", async () => {
  const result = await loadCallLanguages(async (path) => {
    if (path === "/call-languages") return catalog;
    throw new Error("preferences unavailable");
  });
  assert.equal(result.selected, "");
  assert.equal(result.choices.length, catalog.items.length);
  assert.match(result.warning, /Choose a spoken language/);
  const malformed = await loadCallLanguages(async (path) =>
    path === "/call-languages" ? catalog : { spoken_language: 3 },
  );
  assert.equal(malformed.selected, "");
  await assert.rejects(
    loadCallLanguages(async (path) => {
      if (path === "/call-languages") throw new Error("catalog unavailable");
      return { spoken_language: "en" };
    }),
    /catalog unavailable/,
  );
});
