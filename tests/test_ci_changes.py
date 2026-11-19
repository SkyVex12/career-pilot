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


def test_desktop_packaging_scripts_run_their_node_tests_without_deploying() -> None:
    assert runs("apps/desktop/scripts/release-checks.mjs") == {"typescript"}
    assert runs("apps/desktop/scripts/release.mjs") == {"windows", "typescript"}


def test_the_gateway_runs_python_the_linux_client_tests_and_a_deploy() -> None:
    # The client core's integration tests start the real gateway.
    assert runs("apps/gateway/src/copilot_gateway/session.py") == {"python", "rust_linux", "server"}


def test_the_api_and_worker_deploy_without_the_windows_job() -> None:
    assert runs("apps/api/src/copilot_api/main.py") == {"python", "server"}
    assert runs("migrations/0008_x.sql") == {"python", "server"}


def test_the_schema_runs_every_language_and_deploys() -> None:
    assert runs("packages/protocol/protocol.schema.json") == {
        "windows", "rust_linux", "python", "typescript", "codegen", "server",
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
    assert runs("uv.lock") == {"rust_linux", "python", "typescript", "codegen", "server"}
    assert runs("pnpm-lock.yaml") == {"typescript", "codegen"}


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/deploy.yml",
        ".github/workflows/desktop-macos.yml",
        ".github/workflows/release-desktop.yml",
    ],
)
def test_workflow_edits_run_lightweight_checks_without_rebuilding_the_app(path: str) -> None:
    assert runs(path) == {"tooling", "workflows"}
    # Neither ordering may suppress a real server change alongside workflow edits.
    mixed = {"tooling", "workflows", "python", "server"}
    assert runs(path, "apps/api/src/copilot_api/main.py") == mixed
    assert runs("apps/api/src/copilot_api/main.py", path) == mixed


@pytest.mark.parametrize("path", cc.TOOLING_FILES)
def test_standalone_planners_do_not_start_backend_services(path: str) -> None:
    assert runs(path) == {"tooling"}


def test_mixed_tooling_and_backend_changes_keep_backend_validation() -> None:
    assert runs("scripts/ci_changes.py", "apps/api/src/copilot_api/main.py") == {
        "tooling", "python", "server",
    }  # fmt: skip
    assert runs("scripts/desktop_release.py", "apps/desktop/src/App.tsx") == {
        "tooling", "typescript",
    }  # fmt: skip


@pytest.mark.parametrize(
    "path",
    [
        "packages/protocol/protocol.schema.json",
        "packages/protocol/python/copilot_protocol/models.py",
        "packages/protocol/src/generated.ts",
        "packages/protocol/rust/src/generated.rs",
        "packages/protocol/scripts/codegen-ts.mjs",
        "packages/protocol/scripts/codegen_rs.py",
        "scripts/codegen.py",
        *cc.PYTHON_PROJECT,
        *cc.NODE_PROJECT,
    ],
)
def test_codegen_inputs_and_generated_files_always_check_regeneration(path: str) -> None:
    assert {"codegen", "typescript"} <= runs(path)


def test_backend_helpers_still_need_the_stack() -> None:
    assert runs("scripts/desktop_test_stack.py") == {"python"}
    assert runs("tests/test_end_to_end.py") == {"python"}


def test_unknown_files_run_everything() -> None:
    assert runs("some/new/thing.txt") == ALL


def test_every_tracked_file_is_known() -> None:
    """A new top-level file runs everything until it is placed in an area."""
    files = subprocess.run(
        ["git", "ls-files"], cwd=SCRIPT.parent.parent, capture_output=True, text=True, check=True
    ).stdout.split()
    everything = [f for f in files if runs(f) == ALL]
    assert everything == []


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert cc.main(["--all", "--github-output"]) == 0
    assert "windows=true\n" in out.read_text(encoding="utf-8")
    assert "server=true\n" in out.read_text(encoding="utf-8")
    assert "codegen=true\n" in out.read_text(encoding="utf-8")
    assert "tooling=true\n" in out.read_text(encoding="utf-8")


def test_failed_diff_runs_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_diff(base: str, head: str) -> list[str]:
        raise subprocess.CalledProcessError(1, "git diff", stderr="missing base")

    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(cc, "changed", fail_diff)
    assert cc.main(["--base", "missing", "--github-output"]) == 0
    assert set(out.read_text(encoding="utf-8").splitlines()) == {f"{area}=true" for area in ALL}
