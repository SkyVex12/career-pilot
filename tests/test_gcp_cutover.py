"""Deployment safety without cloud access: real shell control flow with fake workloads."""

import base64
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "infra/gcp/host/copilotctl.sh"
spec = importlib.util.spec_from_file_location("gcp_stage", ROOT / "infra/gcp/scripts/stage.py")
assert spec is not None
assert spec.loader is not None
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def metadata(script: bytes) -> str:
    return (
        "#cloud-config\nwrite_files:\n  - path: /var/lib/copilot/copilotctl.sh\n"
        '    permissions: "0700"\n    owner: root\n    encoding: b64\n'
        f"    content: {base64.b64encode(script).decode()}\n"
        "  - path: /unchanged\n    content: keep-me\n"
    )


def test_prepare_changes_only_embedded_boot_helper():
    before = metadata(b"old script")
    updated = stage.boot_metadata(b"new script", before)
    assert updated == metadata(b"new script")
    with pytest.raises(ValueError, match="exactly one"):
        stage.boot_metadata(b"new", "#cloud-config\n")


def test_ci_refuses_stale_boot_helper_before_install(monkeypatch):
    remote = Mock()
    monkeypatch.setattr(stage, "host_script", lambda: b"new")
    monkeypatch.setattr(stage, "current_boot_metadata", lambda: metadata(b"old"))
    monkeypatch.setattr(stage, "on_vm", remote)
    with pytest.raises(SystemExit, match="prepare-host"):
        stage.install_host_helper()
    remote.assert_not_called()


def test_metadata_lookup_uses_read_permission_only(monkeypatch):
    cloud = Mock(
        return_value=json.dumps(
            {
                "metadata": {
                    "items": [
                        {"key": "user-data", "value": metadata(b"helper")},
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(stage, "gcloud", cloud)
    assert stage.current_boot_metadata() == metadata(b"helper")
    assert "describe" in cloud.call_args.args


def test_install_uses_scp_and_short_command_without_metadata_write(monkeypatch):
    script = b"#!/bin/bash\n" + b"# large boot helper\n" * 2000
    monkeypatch.setattr(stage, "host_script", lambda: script)
    monkeypatch.setattr(stage, "current_boot_metadata", lambda: metadata(script))
    monkeypatch.setattr(stage, "IN_CI", True)
    cloud = Mock()
    remote = Mock()
    monkeypatch.setattr(stage, "gcloud", cloud)
    monkeypatch.setattr(stage, "on_vm", remote)
    stage.install_host_helper()
    assert cloud.call_args.args[:2] == ("compute", "scp")
    assert "--ssh-key-expire-after=2h" in cloud.call_args.args
    assert len(remote.call_args.args[0]) < 1024
    assert "sudo bash -n" in remote.call_args.args[0]


def test_failed_health_holds_writers_without_rollback(monkeypatch):
    for name in ("check_images", "install_host_helper", "summary"):
        monkeypatch.setattr(stage, name, Mock())
    monkeypatch.setattr(stage, "ensure_running", lambda start: False)
    monkeypatch.setattr(stage, "deployed_tag", lambda: "old")
    monkeypatch.setattr(stage, "health", lambda: False)
    remote = Mock(return_value=(0, ""))
    monkeypatch.setattr(stage, "on_vm_rc", remote)
    with pytest.raises(SystemExit, match="health check failed"):
        stage.deploy("new", False, True, True)
    assert [c.args[0] for c in remote.call_args_list] == [
        f"{stage.CTL} deploy new --coordinated",
        f"{stage.CTL} hold",
    ]
    assert all(c.kwargs["retry"] is False for c in remote.call_args_list)


def test_uncertain_ssh_does_not_retry_deploy_or_claim_rollback(monkeypatch):
    for name in ("check_images", "install_host_helper", "summary"):
        monkeypatch.setattr(stage, name, Mock())
    monkeypatch.setattr(stage, "ensure_running", lambda start: False)
    monkeypatch.setattr(stage, "deployed_tag", lambda: "old")
    remote = Mock(return_value=(255, ""))
    monkeypatch.setattr(stage, "on_vm_rc", remote)
    with pytest.raises(SystemExit, match="inspect the VM"):
        stage.deploy("new", False, True, True)
    assert remote.call_count == 1
    assert remote.call_args.kwargs["retry"] is False


def shell(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if os.name == "nt" and git_bash.exists() else shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the deployment helper control-flow tests")

    def bash_path(path: Path) -> str:
        value = path.as_posix()
        return "/" + value[0].lower() + value[2:] if os.name == "nt" else value

    setup = f"""
export PATH=/usr/bin:/bin:$PATH
source {shlex.quote(bash_path(HOST))}
REAL_ASSERT_DRAINED=$(declare -f assert_drained)
STATE=.
RUN=$STATE/run
DATA=$STATE
MAINTENANCE=$STATE/maintenance
TRACE=$STATE/trace
mkdir -p "$RUN"
printf 'old\n' > "$STATE/image-tag"
log() {{ printf '%s\n' "log $*" >> "$TRACE"; }}
systemctl() {{ printf '%s\n' "systemctl $*" >> "$TRACE"; }}
freeze_admission() {{ log freeze; }}
restore_admission() {{ log unfreeze; }}
assert_drained() {{ log drained; }}
cutover_backup() {{ log backup; CUTOVER_BACKUP=$STATE/backup; }}
rehearse() {{ log rehearsal; }}
docker_run() {{ log "docker_run $*"; }}
healthy() {{ log "healthy $*"; }}
"""
    return subprocess.run(
        [bash, "-c", setup + body],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cutover_orders_draining_backup_rehearsal_before_live_migration(tmp_path):
    result = shell(tmp_path, "coordinated_deploy new\n")
    assert result.returncode == 0, result.stderr
    trace = (tmp_path / "trace").read_text()
    assert trace.index("log drained") < trace.index("systemctl stop copilot-proxy")
    assert trace.index("log backup") < trace.index("log rehearsal")
    assert trace.index("log rehearsal") < trace.index("log docker_run migrate wait")
    assert trace.index("log docker_run migrate wait") < trace.index("systemctl start copilot-api")
    assert (tmp_path / "image-tag").read_text().strip() == "new"
    assert not (tmp_path / "maintenance").exists()


def test_active_call_aborts_without_stopping_writers_or_migrating(tmp_path):
    result = shell(tmp_path, "assert_drained() { die 'active call'; }\ncoordinated_deploy new\n")
    assert result.returncode != 0
    trace = (tmp_path / "trace").read_text()
    assert "log unfreeze" in trace
    assert "systemctl stop copilot-proxy" not in trace
    assert "docker_run migrate" not in trace
    assert not (tmp_path / "maintenance").exists()


@pytest.mark.parametrize("pending", ["lease", "log"])
def test_drain_refuses_disconnected_leases_and_retained_logs(tmp_path, pending):
    result = shell(
        tmp_path,
        """
eval "$REAL_ASSERT_DRAINED"
ss() { :; }
docker() {
  case "$*" in
    *"sess:*:lease"*) [ "$PENDING" != lease ] || echo 'sess:{test}:lease' ;;
    *"sess:*:log"*) echo 'sess:{test}:log' ;;
    *"XLEN"*) echo 1 ;;
  esac
  return 0
}
"""
        + f"PENDING={pending}\nassert_drained\n",
    )
    assert result.returncode != 0
    assert ("session leases remain" if pending == "lease" else "retained session logs remain") in (
        tmp_path / "trace"
    ).read_text()


@pytest.mark.parametrize("failure", ["rehearsal", "migration", "health"])
def test_failures_never_put_old_images_over_a_partially_advanced_schema(tmp_path, failure):
    override = {
        "rehearsal": "rehearse() { return 1; }",
        "migration": "docker_run() { return 1; }",
        "health": "healthy() { return 1; }",
    }[failure]
    result = shell(tmp_path, override + "\ncoordinated_deploy new\n")
    assert result.returncode != 0
    if failure == "rehearsal":
        assert (tmp_path / "image-tag").read_text().strip() == "old"
        assert not (tmp_path / "maintenance").exists()
    else:
        assert (tmp_path / "image-tag").read_text().strip() == "new"
        assert (tmp_path / "maintenance").exists()
        assert not (tmp_path / "run/cutover-start").exists()
        trace = (tmp_path / "trace").read_text()
        assert "no image-only rollback is safe" in trace


def test_boot_keeps_app_and_scheduler_stopped_during_hold(tmp_path):
    result = shell(
        tmp_path,
        """
mount_data() { :; }
firewall() { :; }
swap() { :; }
registry() { echo example/repo; }
docker-credential-gcr() { :; }
mkdir() { :; }
touch "$MAINTENANCE"
boot
""",
    )
    assert result.returncode == 0, result.stderr
    trace = (tmp_path / "trace").read_text()
    assert "systemctl start copilot-postgres copilot-valkey copilot-elasticmq" in trace
    assert "copilot-scheduler.timer" not in trace
    assert "systemctl start copilot-proxy" not in trace


@pytest.mark.parametrize("compatible", [False, True])
def test_deploy_rejects_old_schema_or_unapproved_migrations_before_tag_change(tmp_path, compatible):
    result = shell(
        tmp_path,
        """
prune_images() { :; }
registry() { echo example/repo; }
docker() { log "docker $*"; }
wait_data() { :; }
write_env() { :; }
"""
        + (
            "candidate_database() { echo 1; }"
            if compatible
            else "candidate_database() { return 1; }"
        )
        + "\ndeploy new\n",
    )
    assert result.returncode != 0
    assert (tmp_path / "image-tag").read_text().strip() == "old"
    trace = (tmp_path / "trace").read_text()
    assert "docker pull" in trace
    assert "docker_run migrate" not in trace
    assert "systemctl stop" not in trace
