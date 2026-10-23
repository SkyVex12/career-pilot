"""Desktop release planning (README.md, "Releasing the desktop app"), for release-desktop.yml.

    python scripts/desktop_release.py plan --sha <commit> [--tag desktop-v0.2.0] [--published]
        [--published-version 0.1.3] [--github-output]
    python scripts/desktop_release.py newer <a> <b>     # exit 0 if version a > b, else 1

`plan` decides whether a commit on main gets a new installer and with which version:

- After a merge (no --tag): a release only if something the app ships changed since the last
  desktop-v* tag (apps/desktop, the protocol's Rust and TypeScript, the lockfiles, the release
  workflow), skipped when only the backend or docs changed. The version is the next patch
  after the newest of every desktop-v* tag and the version in the published latest.json (so a
  release whose tag push failed is not reused). A commit older than the newest tag is skipped.
- For a tag pushed by hand (--tag): always a release, with the tag's version.

The notes list the pull requests merged since the previous release that changed the app,
from the merge commits' titles (or squash merges' subjects). Everything comes from git, so a
test can run it against a throwaway repository (tests/test_desktop_release.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_PREFIX = "desktop-v"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")

# What the installer is built from. A path ships if it starts with one of these and is not
# a test, a doc, or the protocol's Python side.
SHIPPED_PREFIXES = (
    "apps/desktop/",
    "packages/protocol/",
    "Cargo.toml",
    "Cargo.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "rust-toolchain.toml",
    ".github/workflows/release-desktop.yml",
)
NOT_SHIPPED_PREFIXES = (
    "packages/protocol/python/",
    "packages/protocol/fixtures/",
    "packages/protocol/pyproject.toml",
)
NOT_SHIPPED_PARTS = ("tests", "__pycache__")
NOT_SHIPPED_SUFFIXES = (".md",)

MERGE_PR = re.compile(r"^Merge pull request #(\d+) from ")
SQUASH_PR = re.compile(r"\(#(\d+)\)$")


Version = tuple[int, int, int, tuple[int | str, ...] | None]


def parse_version(text: str) -> Version | None:
    m = SEMVER.match(text.strip().removeprefix(TAG_PREFIX).removeprefix("v"))
    if m is None:
        return None
    pre = None
    if m.group(4):
        pre = tuple(int(p) if p.isdigit() else p for p in m.group(4).split("."))
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def version_key(v: Version) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """Semver order: a prerelease sorts before its release."""
    major, minor, patch, pre = v
    if pre is None:
        return major, minor, patch, 1, ()
    return major, minor, patch, 0, tuple((0, p) if isinstance(p, int) else (1, p) for p in pre)


def newer(a: str, b: str) -> bool:
    va, vb = parse_version(a), parse_version(b)
    if va is None or vb is None:
        raise ValueError(f"not a version: {a if va is None else b}")
    return version_key(va) > version_key(vb)


def format_version(v: Version) -> str:
    major, minor, patch, pre = v
    base = f"{major}.{minor}.{patch}"
    return base if pre is None else base + "-" + ".".join(str(p) for p in pre)


def next_patch(versions: list[str]) -> str:
    """The patch after the newest release; 0.1.0 when there is none. A prerelease 1.2.0-rc.1
    is followed by 1.2.0."""
    parsed = [v for v in map(parse_version, versions) if v is not None]
    if not parsed:
        return "0.1.0"
    major, minor, patch, pre = max(parsed, key=version_key)
    return f"{major}.{minor}.{patch}" if pre is not None else f"{major}.{minor}.{patch + 1}"


def ships(path: str) -> bool:
    if not path.startswith(SHIPPED_PREFIXES) or path.startswith(NOT_SHIPPED_PREFIXES):
        return False
    if path.endswith(NOT_SHIPPED_SUFFIXES):
        return False
    return not any(part in NOT_SHIPPED_PARTS for part in path.split("/")[:-1])


class Git:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def __call__(self, *args: str, check: bool = True) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.cwd, capture_output=True, text=True, encoding="utf-8"
        )
        if check and done.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout if done.returncode == 0 else ""

    def ok(self, *args: str) -> bool:
        return subprocess.run(["git", *args], cwd=self.cwd, capture_output=True).returncode == 0

    def rev(self, ref: str) -> str:
        return self("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def tags(self, merged_into: str | None = None) -> list[str]:
        args = ["tag", "--list", f"{TAG_PREFIX}*"]
        if merged_into:
            args += ["--merged", merged_into]
        return [t for t in self(*args).split() if parse_version(t) is not None]

    def changed(self, base: str | None, head: str) -> list[str]:
        if base is None:
            return self("ls-tree", "-r", "--name-only", head).split("\n")
        return [p for p in self("diff", "--name-only", f"{base}..{head}").split("\n") if p]

    def first_parent_commits(self, base: str | None, head: str) -> list[str]:
        span = head if base is None else f"{base}..{head}"
        return self("rev-list", "--first-parent", "--reverse", span).split()


def newest(tags: list[str]) -> str | None:
    """The newest release tag (prereleases count, so a hand-pushed rc is a base too)."""
    parsed = [(t, parse_version(t)) for t in tags]
    valid = [(t, v) for t, v in parsed if v is not None]
    return max(valid, key=lambda tv: version_key(tv[1]))[0] if valid else None


def release_notes(git: Git, base: str | None, head: str, version: str) -> str:
    """One line per pull request merged into main since `base` that changed what ships."""
    lines: list[str] = []
    others: list[str] = []
    for sha in git.first_parent_commits(base, head):
        parents = git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        if parents:
            files = git("diff", "--name-only", parents[0], sha).split("\n")
        else:
            files = git("show", "--name-only", "--format=", sha).split("\n")
        if not any(ships(f) for f in files if f):
            continue
        subject, _, body = git("log", "-1", "--format=%s%n%b", sha).partition("\n")
        subject = subject.strip()
        if m := MERGE_PR.match(subject):
            title = next((ln.strip() for ln in body.splitlines() if ln.strip()), subject)
            lines.append(f"- {title} (#{m.group(1)})")
        elif SQUASH_PR.search(subject):
            lines.append(f"- {subject}")
        else:
            others.append(f"- {subject}")
    # Pushed straight to main: listed only when no pull request was merged.
    items = lines or others
    return "\n".join([f"Copilot {version}", "", *items]) if items else f"Copilot {version}"


@dataclass
class Plan:
    release: bool
    reason: str
    sha: str
    version: str = ""
    previous: str = ""
    create_tag: bool = False
    notes: str = ""

    def outputs(self) -> dict[str, str]:
        return {
            "release": "true" if self.release else "false",
            "reason": self.reason,
            "sha": self.sha,
            "version": self.version,
            "previous": self.previous,
            "create_tag": "true" if self.create_tag else "false",
            "notes": self.notes,
        }


def plan(git: Git, sha: str, tag: str | None = None, published_version: str | None = None) -> Plan:
    head = git.rev(sha)
    if tag:
        v = parse_version(tag)
        if v is None:
            raise ValueError(f"tag {tag}: expected {TAG_PREFIX}1.2.3")
        version = format_version(v)
        earlier = [t for t in git.tags(merged_into=head) if t != tag and not newer(t, version)]
        base = newest(earlier)
        notes = release_notes(git, base, head, version)
        return Plan(True, f"tag {tag}", head, version, base or "", create_tag=False, notes=notes)

    every = git.tags()
    latest = newest(every)
    if latest is not None and not git.ok("merge-base", "--is-ancestor", latest, head):
        return Plan(False, f"{head[:12]} is not newer than {latest}; no release", head)
    base = newest(git.tags(merged_into=head))
    changed = [p for p in git.changed(base, head) if ships(p)]
    if base is not None and not changed:
        return Plan(False, f"nothing the app ships changed since {base}", head, previous=base)
    known = every + ([published_version] if published_version else [])
    version = next_patch(known)
    notes = release_notes(git, base, head, version)
    shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
    reason = f"changed since {base}: {shown}" if base else "first release"
    return Plan(True, reason, head, version, base or "", create_tag=True, notes=notes)


def published(release_json: Path) -> str | None:
    """The version in the published latest.json, or None if it cannot be read."""
    try:
        url = json.loads(release_json.read_text(encoding="utf-8"))["update_url"]
        with urllib.request.urlopen(url, timeout=15) as response:
            return str(json.load(response)["version"])
    except Exception as e:
        print(f"latest.json not read ({e}); using the tags only", file=sys.stderr)
        return None


def write_github_output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise SystemExit("--github-output: GITHUB_OUTPUT is not set")
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            delimiter = "EOF_" + os.urandom(8).hex()
            f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--sha", required=True)
    p.add_argument("--tag", default="")
    p.add_argument("--repo", type=Path, default=ROOT)
    p.add_argument("--published", action="store_true", help="read latest.json's version")
    p.add_argument("--published-version", default="")
    p.add_argument("--github-output", action="store_true")
    n = sub.add_parser("newer")
    n.add_argument("a")
    n.add_argument("b")
    args = parser.parse_args(argv)

    if args.command == "newer":
        return 0 if newer(args.a, args.b) else 1

    pub = args.published_version or None
    if args.published and pub is None:
        pub = published(args.repo / "apps" / "desktop" / "release.json")
    result = plan(Git(args.repo), args.sha, args.tag or None, pub)
    print(json.dumps(result.outputs(), indent=2))
    if args.github_output:
        write_github_output(result.outputs())
    return 0


if __name__ == "__main__":
    sys.exit(main())
