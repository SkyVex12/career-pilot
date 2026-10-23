"""Which CI jobs a change needs (ci.yml's `changes` job, and deploy.yml's), to save Actions minutes.

    python scripts/ci_changes.py --base <commit> --head <commit> [--github-output]
    python scripts/ci_changes.py --all [--github-output]        # everything (manual runs)

Each area lists what can affect it; a change outside every area runs everything (an unknown
file is never a reason to skip). Changes to CI itself, or to this script, run everything too.

- windows:    the desktop shell's Windows job (clippy and tests of src-tauri): the shell, the
              client core it links, the protocol's Rust, the Cargo files
- rust_linux: the client core and the protocol crate on Linux; the core's integration tests
              start the real gateway, so the gateway and the Python it imports count too
- python:     lint, types and tests of every Python package (the services, evals, scripts)
- typescript: the desktop UI and the protocol's TypeScript and codegen
- terraform:  infra/
- server:     what deploy.yml builds into images and deploys to the VM; a merge without it
              (desktop, docs, Terraform only) builds no image and deploys nothing
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EVERYTHING = (".github/workflows/ci.yml", ".github/workflows/deploy.yml", "scripts/ci_changes.py")

CARGO = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "rustfmt.toml")
PYTHON_PROJECT = ("pyproject.toml", "uv.lock", ".python-version")
NODE_PROJECT = ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "tsconfig.base.json")

AREAS: dict[str, tuple[str, ...]] = {
    "windows": (
        "apps/desktop/src-tauri/",
        "apps/desktop/core/",
        "apps/desktop/release.json",
        "apps/desktop/scripts/release.mjs",
        "packages/protocol/rust/",
        "packages/protocol/protocol.schema.json",
        *CARGO,
    ),
    "rust_linux": (
        "apps/desktop/core/",
        "packages/protocol/",
        "apps/gateway/",
        "packages/core/",
        *CARGO,
        *PYTHON_PROJECT,
    ),
    "python": (
        "apps/api/",
        "apps/gateway/",
        "apps/worker/",
        "packages/core/",
        "packages/protocol/",
        "migrations/",
        "evals/",
        "scripts/",
        "tests/",
        "infra/scripts/",
        "infra/gcp/scripts/",
        "docker/",
        "docker-compose.yml",
        ".env.example",
        *PYTHON_PROJECT,
    ),
    "typescript": (
        "apps/desktop/src/",
        "apps/desktop/tests/",
        "apps/desktop/package.json",
        "apps/desktop/index.html",
        "apps/desktop/vite.config.ts",
        "apps/desktop/tsconfig.json",
        "apps/desktop/tsconfig.node.json",
        "packages/protocol/",
        "scripts/codegen.py",
        *NODE_PROJECT,
        *PYTHON_PROJECT,
    ),
    "terraform": ("infra/",),
    "server": (
        "apps/api/",
        "apps/gateway/",
        "apps/worker/",
        "packages/core/",
        "packages/protocol/python/",
        "packages/protocol/protocol.schema.json",
        "packages/protocol/pyproject.toml",
        "migrations/",
        "docker/",
        ".dockerignore",
        *PYTHON_PROJECT,
    ),
}

# Never affect any job: docs, and files only people or other workflows read. (Markdown inside
# a package, like a fixtures README, is documentation too.)
NOTHING = (".md", ".gitignore")
NOTHING_PREFIXES = (
    "docs/",
    ".github/workflows/release-desktop.yml",
    ".gitattributes",
    ".editorconfig",
)


def needs(paths: list[str]) -> dict[str, bool]:
    out = dict.fromkeys(AREAS, False)
    for path in paths:
        if path.startswith(EVERYTHING):
            return dict.fromkeys(AREAS, True)
        if path.endswith(NOTHING) or path.startswith(NOTHING_PREFIXES):
            continue
        hit = [area for area, prefixes in AREAS.items() if path.startswith(prefixes)]
        if not hit:
            # Unknown to every area: run everything rather than guess.
            return dict.fromkeys(AREAS, True)
        for area in hit:
            out[area] = True
    return out


def changed(base: str, head: str) -> list[str]:
    done = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return [p for p in done.stdout.splitlines() if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--all", action="store_true", help="everything (manual runs)")
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args(argv)

    if args.all or not args.base:
        result, paths = dict.fromkeys(AREAS, True), ["(all)"]
    else:
        try:
            paths = changed(args.base, args.head)
        except subprocess.CalledProcessError as e:
            print(f"git diff failed ({e.stderr.strip()}); running everything", file=sys.stderr)
            paths = ["(git diff failed)"]
            result = dict.fromkeys(AREAS, True)
        else:
            result = needs(paths)
    for area, run in result.items():
        print(f"{area}: {'run' if run else 'skip'}")
    print(f"({len(paths)} files: {', '.join(paths[:8])}{' ...' if len(paths) > 8 else ''})")
    if args.github_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            for area, run in result.items():
                f.write(f"{area}={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
