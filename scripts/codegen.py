"""Regenerate the protocol types from packages/protocol/protocol.schema.json.

    uv run python scripts/codegen.py           # rewrite the generated files   (pnpm codegen)
    uv run python scripts/codegen.py --check   # fail if they are out of date  (pnpm codegen:check)

Python types come from datamodel-code-generator, TypeScript types from
json-schema-to-typescript (needs `pnpm install` first). Both are generated into a temp
directory first, so --check never touches the working tree.
"""

import argparse
import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = ROOT / "packages" / "protocol"
SCHEMA = PROTOCOL / "protocol.schema.json"
PY_OUT = PROTOCOL / "python" / "copilot_protocol" / "models.py"
TS_OUT = PROTOCOL / "src" / "generated.ts"

PY_HEADER = (
    "# Generated from protocol.schema.json by datamodel-code-generator."
    " Do not edit; run `pnpm codegen`."
)
DATAMODEL_ARGS = [
    "--input-file-type", "jsonschema",
    "--output-model-type", "pydantic_v2.BaseModel",
    "--target-python-version", "3.12",
    "--use-annotated",
    "--field-constraints",
    "--use-double-quotes",
    "--enum-field-as-literal", "all",
    "--use-schema-description",
    "--use-field-description",
    "--use-standard-collections",
    "--use-union-operator",
    "--collapse-root-models",
    "--skip-root-model",
    "--allow-population-by-field-name",
    "--disable-timestamp",
    "--custom-file-header", PY_HEADER,
    "--formatters", "ruff-format",
]  # fmt: skip


def generate_python(out: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "datamodel_code_generator", "--input", str(SCHEMA),
         "--output", str(out), *DATAMODEL_ARGS],
        check=True,
        cwd=PROTOCOL,
    )  # fmt: skip


def generate_typescript(out: Path) -> None:
    node = shutil.which("node")
    if node is None:
        sys.exit("node not found; install Node.js 22+ and run `pnpm install`")
    if not (PROTOCOL / "node_modules" / "json-schema-to-typescript").exists():
        sys.exit("json-schema-to-typescript not installed; run `pnpm install` at the repo root")
    subprocess.run(
        [node, str(PROTOCOL / "scripts" / "codegen-ts.mjs"), str(out)], check=True, cwd=PROTOCOL
    )


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate protocol types.")
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        targets = {PY_OUT: Path(tmp, "models.py"), TS_OUT: Path(tmp, "generated.ts")}
        generate_python(targets[PY_OUT])
        generate_typescript(targets[TS_OUT])

        stale: list[Path] = []
        for committed, fresh in targets.items():
            new = read(fresh)
            if read(committed) == new:
                continue
            stale.append(committed)
            if args.check:
                diff = difflib.unified_diff(
                    read(committed).splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"{committed.relative_to(ROOT).as_posix()} (committed)",
                    tofile="(generated)",
                    n=1,
                )
                sys.stdout.writelines(list(diff)[:60])
            else:
                committed.write_bytes(new.encode("utf-8"))  # LF endings on every OS

    names = ", ".join(p.relative_to(ROOT).as_posix() for p in stale)
    if args.check:
        if stale:
            print(f"\nOut of date: {names}. Run `pnpm codegen` and commit the result.")
            return 1
        print("Generated protocol types are up to date.")
    else:
        print(f"Updated: {names}" if stale else "Generated protocol types already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
