"""scripts/ci_changes.py: which CI jobs and whether a deploy each kind of change needs."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_changes.py"
spec = importlib.util.spec_from_file_location("ci_changes", SCRIPT)
assert spec
assert spec.loader
cc = importlib.util.module_from_spec(spec)
sys.modules["ci_changes"] = cc
spec.loader.exec_module(cc)


def runs(*paths: str) -> set[str]:
    return {area for area, run in cc.needs(list(paths)).items() if run}


ALL = set(cc.AREAS)


def test_the_desktop_ui_alone_runs_typescript_only() -> None:
    assert runs("apps/desktop/src/App.tsx", "apps/desktop/tests/updates.test.ts") == {"typescript"}


def test_the_desktop_shell_runs_the_windows_job_and_no_deploy() -> None:
    assert runs("apps/desktop/src-tauri/src/lib.rs") == {"windows"}
    assert runs("apps/desktop/core/src/api.rs") == {"windows", "rust_linux"}


def test_the_gateway_runs_python_the_linux_client_tests_and_a_deploy() -> None:
    # The client core's integration tests start the real gateway.
    assert runs("apps/gateway/src/copilot_gateway/session.py") == {"python", "rust_linux", "server"}


def test_the_api_and_worker_deploy_without_the_windows_job() -> None:
    assert runs("apps/api/src/copilot_api/main.py") == {"python", "server"}
    assert runs("migrations/0008_x.sql") == {"python", "server"}


def test_the_schema_runs_every_language_and_deploys() -> None:
    assert runs("packages/protocol/protocol.schema.json") == {
        "windows", "rust_linux", "python", "typescript", "server",
    }  # fmt: skip


def test_docs_run_nothing() -> None:
    assert (
        runs(
            "README.md",
            "ARCHITECTURE.md",
            "docs/analysis/x.md",
            "apps/worker/tests/fixtures/README.md",
        )
        == set()
    )


def test_terraform_runs_its_check_and_no_deploy() -> None:
    assert runs("infra/gcp/envs/stage0/main.tf") == {"terraform"}
    # stage.py is Python: linted and typed.
    assert runs("infra/gcp/scripts/stage.py") == {"terraform", "python"}


def test_the_lockfiles() -> None:
    assert runs("Cargo.lock") == {"windows", "rust_linux"}
    assert runs("uv.lock") == {"rust_linux", "python", "typescript", "server"}
    assert runs("pnpm-lock.yaml") == {"typescript"}


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/deploy.yml",
        "scripts/ci_changes.py",
        "some/new/thing.txt",
    ],
)
def test_ci_itself_and_unknown_files_run_everything(path: str) -> None:
    assert runs(path) == ALL


def test_every_tracked_file_is_known() -> None:
    """A new top-level file runs everything until it is placed in an area."""
    files = subprocess.run(
        ["git", "ls-files"], cwd=SCRIPT.parent.parent, capture_output=True, text=True, check=True
    ).stdout.split()
    everything = [f for f in files if not f.startswith(cc.EVERYTHING) and runs(f) == ALL]
    assert everything == []


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert cc.main(["--all", "--github-output"]) == 0
    assert "windows=true\n" in out.read_text(encoding="utf-8")
    assert "server=true\n" in out.read_text(encoding="utf-8")
