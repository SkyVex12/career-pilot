export type Choice = {
  value: string;
  label: string;
  description?: string;
  nativeLabel?: string;
  nativeDirection?: "ltr" | "rtl";
};

function searchable(value: string): string {
  return value.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase();
}

/** Match names, native script, and stable codes without requiring accent input. */
export function filterChoices(options: Choice[], query: string): Choice[] {
  const words = searchable(query.trim()).split(/\s+/).filter(Boolean);
  if (!words.length) return options;
  return options.filter((option) => {
    const text = searchable(
      `${option.label} ${option.nativeLabel ?? ""} ${option.description ?? ""} ${option.value}`,
    );
    return words.every((word) => text.includes(word));
  });
}
