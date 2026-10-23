"""scripts/desktop_release.py: the release-on-merge decision of release-desktop.yml, run
against a throwaway git repository shaped like this one (a dry run of the workflow's logic)."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "desktop_release.py"
spec = importlib.util.spec_from_file_location("desktop_release", SCRIPT)
assert spec
assert spec.loader
dr = importlib.util.module_from_spec(spec)
sys.modules["desktop_release"] = dr
spec.loader.exec_module(dr)


class Repo:
    """A main branch with merge commits the way GitHub makes them."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "ci@example.com")
        self.git("config", "user.name", "CI")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.prs = 0

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.path, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            p = self.path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.git("add", "-A")

    def commit(self, message: str, files: dict[str, str]) -> str:
        self.write(files)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge_pr(self, title: str, files: dict[str, str]) -> str:
        self.prs += 1
        branch = f"pr-{self.prs}"
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"work for {title}", files)
        self.git("checkout", "-q", "main")
        self.git(
            "merge", "-q", "--no-ff", branch,
            "-m", f"Merge pull request #{self.prs} from SkyVex12/{branch}", "-m", title,
        )  # fmt: skip
        return self.git("rev-parse", "HEAD")

    def squash_pr(self, title: str, files: dict[str, str]) -> str:
        self.prs += 1
        return self.commit(f"{title} (#{self.prs})", files)

    def tag(self, name: str, ref: str = "HEAD") -> None:
        self.git("tag", name, ref)

    def plan(self, sha: str = "HEAD", **kw: str) -> Any:
        return dr.plan(dr.Git(self.path), sha, **kw)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path)
    r.commit("initial", {"apps/desktop/src/App.tsx": "v1", "apps/api/main.py": "v1"})
    r.tag("desktop-v0.1.0")
    return r


def test_a_merge_that_changes_the_app_releases_the_next_patch(repo: Repo) -> None:
    sha = repo.merge_pr(
        "Overlay: scroll the whole conversation", {"apps/desktop/src/App.tsx": "v2"}
    )
    p = repo.plan()
    assert p.release
    assert p.create_tag
    assert (p.version, p.previous, p.sha) == ("0.1.1", "desktop-v0.1.0", sha)
    assert p.notes == "Copilot 0.1.1\n\n- Overlay: scroll the whole conversation (#1)"
    assert "apps/desktop/src/App.tsx" in p.reason


@pytest.mark.parametrize(
    "files",
    [
        {"apps/api/main.py": "v2"},
        {"README.md": "docs", "ARCHITECTURE.md": "docs", "apps/desktop/README.md": "docs"},
        {"apps/desktop/tests/session.test.ts": "t", "apps/desktop/core/tests/gateway.rs": "t"},
        {"packages/protocol/python/copilot_protocol/models.py": "py"},
        {"infra/gcp/envs/stage0/main.tf": "tf", ".github/workflows/deploy.yml": "ci"},
    ],
    ids=["backend", "docs", "tests", "protocol-python", "infra"],
)
def test_backend_docs_and_tests_only_skip(repo: Repo, files: dict[str, str]) -> None:
    repo.merge_pr("Backend only", files)
    p = repo.plan()
    assert not p.release
    assert not p.create_tag
    assert p.reason == "nothing the app ships changed since desktop-v0.1.0"


@pytest.mark.parametrize(
    "path",
    [
        "packages/protocol/protocol.schema.json",
        "packages/protocol/rust/src/generated.rs",
        "packages/protocol/src/generated.ts",
        "apps/desktop/release.json",
        "apps/desktop/src-tauri/src/release.rs",
        "apps/desktop/core/src/net/close.rs",
        "Cargo.lock",
        "pnpm-lock.yaml",
        ".github/workflows/release-desktop.yml",
    ],
)
def test_what_ships(path: str) -> None:
    assert dr.ships(path)


def test_changes_since_the_last_release_add_up(repo: Repo) -> None:
    # A desktop change whose release never happened (its deploy was replaced by a newer one),
    # then a backend-only merge: the second still releases, with both in the notes.
    repo.merge_pr("Desktop: new settings", {"apps/desktop/src/App.tsx": "v2"})
    repo.merge_pr("API: faster", {"apps/api/main.py": "v2"})
    repo.squash_pr("Protocol: a new close code", {"packages/protocol/protocol.schema.json": "{}"})
    p = repo.plan()
    assert p.release
    assert p.version == "0.1.1"
    assert p.notes.splitlines()[2:] == [
        "- Desktop: new settings (#1)",
        "- Protocol: a new close code (#3)",
    ]


def test_a_tag_pushed_by_hand_sets_the_base_and_the_numbering(repo: Repo) -> None:
    repo.merge_pr("Desktop: big change", {"apps/desktop/src/App.tsx": "v2"})
    repo.tag("desktop-v0.2.0")
    p = repo.plan(tag="desktop-v0.2.0")
    assert p.release
    assert not p.create_tag
    assert (p.version, p.previous) == ("0.2.0", "desktop-v0.1.0")
    assert "Desktop: big change (#1)" in p.notes
    # The next merge that changes the app: 0.2.1, from the hand-pushed tag.
    repo.merge_pr("Desktop: small fix", {"apps/desktop/src/App.tsx": "v3"})
    p = repo.plan()
    assert (p.version, p.previous) == ("0.2.1", "desktop-v0.2.0")
    assert p.notes == "Copilot 0.2.1\n\n- Desktop: small fix (#2)"


def test_a_published_version_without_its_tag_is_not_reused(repo: Repo) -> None:
    # 0.1.1 was published but its tag push failed: the next release is 0.1.2.
    repo.merge_pr("Desktop", {"apps/desktop/src/App.tsx": "v2"})
    assert repo.plan(published_version="0.1.1").version == "0.1.2"
    assert repo.plan(published_version="0.0.9").version == "0.1.1"


def test_an_older_commit_is_never_released(repo: Repo) -> None:
    old = repo.merge_pr("Desktop", {"apps/desktop/src/App.tsx": "v2"})
    repo.merge_pr("Desktop again", {"apps/desktop/src/App.tsx": "v3"})
    repo.tag("desktop-v0.1.1")
    p = repo.plan(old)  # deploy.yml run by hand with an older SHA
    assert not p.release
    assert "is not newer than desktop-v0.1.1" in p.reason


def test_the_released_commit_itself_has_nothing_new(repo: Repo) -> None:
    assert not repo.plan().release


def test_commits_pushed_straight_to_main_are_listed_without_prs(repo: Repo) -> None:
    repo.commit("Desktop: hotfix the hotkey", {"apps/desktop/src/App.tsx": "v2"})
    assert repo.plan().notes == "Copilot 0.1.1\n\n- Desktop: hotfix the hotkey"


def test_versions() -> None:
    assert dr.next_patch(["desktop-v0.1.0", "desktop-v0.1.10", "desktop-v0.1.9"]) == "0.1.11"
    assert dr.next_patch(["desktop-v0.2.0-rc.1", "desktop-v0.1.4"]) == "0.2.0"
    assert dr.next_patch([]) == "0.1.0"
    assert dr.newer("0.1.10", "0.1.9")
    assert not dr.newer("0.1.0", "0.1.0")
    assert dr.newer("0.2.0", "0.2.0-rc.2")
    assert dr.newer("0.2.0-rc.10", "0.2.0-rc.9")
    assert dr.parse_version("desktop-v1.2") is None


def test_github_output_and_cli(repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.merge_pr("Desktop: multi\nline", {"apps/desktop/src/App.tsx": "v2"})
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert dr.main(["plan", "--sha", "HEAD", "--repo", str(repo.path), "--github-output"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "release<<EOF_" in text
    assert "\ntrue\n" in text
    assert "\n0.1.1\n" in text
    assert dr.main(["newer", "0.1.1", "0.1.0"]) == 0
    assert dr.main(["newer", "0.1.0", "0.1.1"]) == 1
