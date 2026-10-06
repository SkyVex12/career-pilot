"""Generate Rust types from protocol.schema.json: rust/src/generated.rs.

    python packages/protocol/scripts/codegen_rs.py [output-path]   (run via `pnpm codegen`)

There is no Rust generator that turns this schema's discriminated unions into serde tagged
enums and also emits its constraints, so this one is written for the subset the schema uses:
objects with `properties` and `required`, string and integer enums, `const`, `$ref`, arrays,
the `uuid` and `date-time` formats, and minimum, maximum, minLength and maxLength. Anything
else stops generation with an error rather than producing a type that is looser than the
schema. Serde checks the structure and types; the generated `Validate` impls check the rest,
and `parse_client` / `parse_server` in lib.rs run both.

The output is formatted by this script (the crate marks the module `#[rustfmt::skip]`), so
`pnpm codegen:check` needs no Rust toolchain.
"""

import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "protocol.schema.json"
DEFAULT_OUT = ROOT / "rust" / "src" / "generated.rs"

HEADER = (
    "// Generated from protocol.schema.json by packages/protocol/scripts/codegen_rs.py."
    " Do not edit; run `pnpm codegen`."
)
UNIONS = ("ClientMessage", "ServerMessage")
RUST_KEYWORDS = {
    "as", "break", "const", "continue", "crate", "else", "enum", "extern", "false", "fn", "for",
    "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
    "self", "static", "struct", "super", "trait", "true", "type", "unsafe", "use", "where",
    "while", "async", "await", "dyn", "abstract", "become", "box", "do", "final", "macro",
    "override", "priv", "typeof", "unsized", "virtual", "yield", "try",
}  # fmt: skip
KNOWN_KEYS = {
    "description", "type", "properties", "required", "enum", "const", "$ref", "items", "format",
    "minimum", "maximum", "minLength", "maxLength", "oneOf", "discriminator", "title",
    "contentEncoding", "contentMediaType", "x-binary-layout", "x-enum-varnames",
}  # fmt: skip
U32_MAX = 4294967295


class SchemaError(Exception):
    pass


def pascal(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def doc(text: str | None, indent: str) -> list[str]:
    if not text:
        return []
    width = 100 - len(indent) - 4
    return [f"{indent}/// {line}".rstrip() for line in textwrap.wrap(text, width)]


def rust_str(value: str) -> str:
    return json.dumps(value)  # JSON string escapes are valid Rust string escapes here


class Generator:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.defs: dict[str, dict[str, Any]] = schema["$defs"]
        self.out: list[str] = []
        self.inline_enums: list[tuple[str, dict[str, Any]]] = []

    # Kinds of definitions.

    def is_string_enum(self, s: dict[str, Any]) -> bool:
        return s.get("type") == "string" and "enum" in s

    def is_object(self, s: dict[str, Any]) -> bool:
        return s.get("type") == "object" and "properties" in s

    def ref_name(self, s: dict[str, Any]) -> str:
        ref: str = s["$ref"]
        if not ref.startswith("#/$defs/"):
            raise SchemaError(f"unsupported $ref {ref}")
        return ref.removeprefix("#/$defs/")

    def check_keys(self, where: str, s: dict[str, Any]) -> None:
        unknown = set(s) - KNOWN_KEYS
        if unknown:
            raise SchemaError(f"{where}: unsupported keywords {sorted(unknown)}")

    # Field types and their checks. A check is Rust code over `v`, a reference to the value.

    def field(self, owner: str, name: str, s: dict[str, Any]) -> tuple[str, list[str], bool]:
        """Rust type, value checks, and whether the value is a struct with its own Validate."""
        self.check_keys(f"{owner}.{name}", s)
        if "$ref" in s:
            target = self.ref_name(s)
            d = self.defs[target]
            if self.is_object(d) or self.is_string_enum(d):
                return target, [], self.is_object(d)
            # Named scalars (Uuid, Seq): their constraints apply at each use.
            return self.field(owner, name, {**d, "description": None})
        t = s.get("type")
        f = rust_str(name)
        if t == "string":
            if "enum" in s:
                enum_name = owner + pascal(name)
                self.inline_enums.append((enum_name, s))
                return enum_name, [], False
            checks: list[str] = []
            if "const" in s:
                checks.append(f"check::const_str({f}, v, {rust_str(s['const'])})?;")
            fmt = s.get("format")
            if fmt == "uuid":
                return "Uuid", [], False
            if fmt == "date-time":
                checks.append(f"check::date_time({f}, v)?;")
            elif fmt is not None:
                raise SchemaError(f"{owner}.{name}: unsupported format {fmt}")
            if "minLength" in s:
                checks.append(f"check::min_len({f}, v, {s['minLength']})?;")
            if "maxLength" in s:
                checks.append(f"check::max_len({f}, v, {s['maxLength']})?;")
            return "String", checks, False
        if t == "integer":
            lo, hi = s.get("minimum"), s.get("maximum")
            if "enum" in s:
                values: list[int] = s["enum"]
                rust = "u8" if max(values) <= 255 and min(values) >= 0 else "i64"
                listed = ", ".join(str(x) for x in values)
                return rust, [f"check::one_of({f}, u64::from(*v), &[{listed}])?;"], False
            if "const" in s:
                return "u32", [f"check::const_int({f}, u64::from(*v), {s['const']})?;"], False
            if lo is None or lo < 0:
                raise SchemaError(f"{owner}.{name}: signed integers are not used by the protocol")
            checks = []
            if hi is not None and hi <= 255:
                rust = "u8"
            elif hi == U32_MAX:
                rust, hi = "u32", None  # the type is the bound
            else:
                rust = "u64"
            if lo > 0:
                checks.append(f"check::min_int({f}, u64::from(*v), {lo})?;")
            if hi is not None:
                checks.append(f"check::max_int({f}, u64::from(*v), {hi})?;")
            return rust, checks, False
        if t == "number":
            checks = []
            if "minimum" in s:
                checks.append(f"check::min_num({f}, *v, {float(s['minimum'])!r})?;")
            if "maximum" in s:
                checks.append(f"check::max_num({f}, *v, {float(s['maximum'])!r})?;")
            return "f64", checks, False
        if t == "boolean":
            return "bool", [], False
        if t == "array":
            inner, checks, nested = self.field(owner, name, s["items"])
            if checks:
                raise SchemaError(f"{owner}.{name}: checks on array items are not supported")
            return f"Vec<{inner}>", [], nested
        raise SchemaError(f"{owner}.{name}: unsupported schema {s}")

    # Emitters.

    def emit(self, *lines: str) -> None:
        self.out.extend(lines)

    def string_enum(self, name: str, s: dict[str, Any]) -> None:
        values: list[str] = s["enum"]
        self.emit(*doc(s.get("description"), ""))
        self.emit(
            "#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]",
            f"pub enum {name} {{",
        )
        for v in values:
            self.emit(f"    #[serde(rename = {rust_str(v)})]", f"    {pascal(v)},")
        self.emit("}", "", f"impl {name} {{", "    pub fn as_str(self) -> &'static str {")
        self.emit("        match self {")
        for v in values:
            self.emit(f"            Self::{pascal(v)} => {rust_str(v)},")
        self.emit("        }", "    }", "}", "")

    def int_enum(self, name: str, s: dict[str, Any]) -> None:
        values: list[int] = s["enum"]
        names: list[str] = s.get("x-enum-varnames") or []
        if len(names) != len(values):
            raise SchemaError(f"{name}: an integer enum needs x-enum-varnames, one per value")
        self.emit(*doc(s.get("description"), ""))
        self.emit("#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]", f"pub enum {name} {{")
        for n, v in zip(names, values, strict=True):
            self.emit(f"    {n} = {v},")
        self.emit("}", "", f"impl {name} {{", f"    pub const ALL: [Self; {len(values)}] = [")
        self.emit(*(f"        Self::{n}," for n in names), "    ];", "")
        self.emit("    pub fn code(self) -> u16 {", "        self as u16", "    }", "")
        self.emit("    pub fn from_code(code: u16) -> Option<Self> {", "        match code {")
        for n, v in zip(names, values, strict=True):
            self.emit(f"            {v} => Some(Self::{n}),")
        self.emit("            _ => None,", "        }", "    }", "}", "")

    def struct(self, name: str, s: dict[str, Any]) -> None:
        self.check_keys(name, s)
        required = set(s.get("required", []))
        fields: list[tuple[str, str, str, list[str], bool, bool, str | None]] = []
        for prop, ps in s["properties"].items():
            if prop == "type" and "const" in ps:
                continue  # the tag of a tagged enum; serde writes it
            rust_type, checks, nested = self.field(name, prop, ps)
            ident = f"{prop}_" if prop in RUST_KEYWORDS else prop
            desc = ps.get("description")
            if desc is None and "$ref" in ps:
                target = self.defs[self.ref_name(ps)]
                if not (self.is_object(target) or self.is_string_enum(target)):
                    desc = target.get("description")
            fields.append((prop, ident, rust_type, checks, nested, prop in required, desc))
        self.emit(*doc(s.get("description"), ""))
        self.emit("#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]")
        if not fields:
            self.emit(f"pub struct {name} {{}}", "")
        else:
            self.emit(f"pub struct {name} {{")
        for prop, ident, rust_type, _, _, req, desc in fields:
            self.emit(*doc(desc, "    "))
            if ident != prop:
                self.emit(f"    #[serde(rename = {rust_str(prop)})]")
            if req:
                self.emit(f"    pub {ident}: {rust_type},")
            else:
                self.emit('    #[serde(default, skip_serializing_if = "Option::is_none")]')
                self.emit(f"    pub {ident}: Option<{rust_type}>,")
        if fields:
            self.emit("}", "")
        body: list[str] = []
        for _, ident, rust_type, checks, nested, req, _ in fields:
            steps = list(checks)
            if nested and rust_type.startswith("Vec<"):
                steps += ["for item in v {", "    item.validate()?;", "}"]
            elif nested:
                steps.append("v.validate()?;")
            if not steps:
                continue
            if req:
                body += ["{", f"    let v = &self.{ident};", *("    " + x for x in steps), "}"]
            else:
                body += [f"if let Some(v) = &self.{ident} {{", *("    " + x for x in steps), "}"]
        self.emit(f"impl Validate for {name} {{")
        if body:
            self.emit("    fn validate(&self) -> Result<(), ValidationError> {")
            self.emit(*("        " + x for x in body))
            self.emit("        Ok(())", "    }", "}", "")
        else:
            self.emit("    fn validate(&self) -> Result<(), ValidationError> {", "        Ok(())")
            self.emit("    }", "}", "")

    def union(self, name: str, s: dict[str, Any]) -> None:
        variants: list[tuple[str, str]] = []
        for ref in s["oneOf"]:
            target = self.ref_name(ref)
            tag = self.defs[target]["properties"]["type"]["const"]
            variants.append((target, tag))
        self.emit(*doc(s.get("description"), ""))
        self.emit(
            "#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]",
            '#[serde(tag = "type")]',
            f"pub enum {name} {{",
        )
        for target, tag in variants:
            self.emit(f"    #[serde(rename = {rust_str(tag)})]", f"    {target}({target}),")
        self.emit("}", "", f"impl {name} {{", "    /// The wire `type` of this message.")
        self.emit("    pub fn type_name(&self) -> &'static str {", "        match self {")
        for target, tag in variants:
            self.emit(f"            Self::{target}(_) => {rust_str(tag)},")
        self.emit("        }", "    }")
        if all("seq" in self.defs[t].get("required", []) for t, _ in variants):
            self.emit("", "    /// The message's `seq`; every variant carries one.")
            self.emit("    pub fn seq(&self) -> u64 {", "        match self {")
            for target, _ in variants:
                self.emit(f"            Self::{target}(m) => m.seq,")
            self.emit("        }", "    }")
        self.emit("}", "")
        self.emit(f"impl Validate for {name} {{")
        self.emit("    fn validate(&self) -> Result<(), ValidationError> {", "        match self {")
        for target, _ in variants:
            self.emit(f"            Self::{target}(m) => m.validate(),")
        self.emit("        }", "    }", "}", "")
        for target, _ in variants:
            self.emit(f"impl From<{target}> for {name} {{")
            self.emit(f"    fn from(m: {target}) -> Self {{", f"        Self::{target}(m)")
            self.emit("    }", "}", "")

    def generate(self) -> str:
        self.emit(
            HEADER,
            "",
            "use serde::{Deserialize, Serialize};",
            "use uuid::Uuid;",
            "",
            "use crate::validate::{self as check, Validate, ValidationError};",
            "",
        )
        for name in UNIONS:
            self.union(name, self.defs[name])
        for name, s in self.defs.items():
            if name in UNIONS:
                continue
            self.check_keys(name, s)
            if self.is_string_enum(s):
                self.string_enum(name, s)
            elif s.get("type") == "integer" and "enum" in s:
                self.int_enum(name, s)
            elif self.is_object(s):
                self.struct(name, s)
            elif s.get("type") in ("string", "integer"):
                continue  # named scalars, inlined at each use
            else:
                raise SchemaError(f"{name}: unsupported definition")
        seen: set[str] = set()
        while self.inline_enums:
            name, s = self.inline_enums.pop(0)
            if name not in seen:
                seen.add(name)
                self.string_enum(name, s)
        return "\n".join(self.out).rstrip() + "\n"


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    schema = json.loads(SCHEMA_PATH.read_text("utf-8"))
    try:
        text = Generator(schema).generate()
    except SchemaError as e:
        print(f"codegen_rs: {e}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
