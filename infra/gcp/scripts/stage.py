"""Stage 0 on GCP, one command per operation (README.md, "Deploying Stage 0").

    uv run infra/gcp/scripts/stage.py <command> [options]

Only the Python standard library and the gcloud CLI (signed in with `gcloud auth login`).
Commands on the VM go through IAP SSH with OS Login, so nothing but port 80 is open; they run
`copilotctl` there (infra/gcp/host/copilotctl.sh). Secret values are read with a hidden prompt
or generated here, piped straight into Secret Manager, and never printed.

    deploy [--tag SHA]     migrations, then each service, on images already in the registry
                           (default: the commit at origin/main, which CI built); --start
                           starts a stopped VM first (and leaves it running), --check runs
                           `health` after and rolls back to the previous tag if it fails
                           (what .github/workflows/deploy.yml runs on every merge)
    rollback               the tag deployed before the current one
    health                 the gateway and the API, on the VM and through nginx
    status                 the deployed tag, the containers and their health, disk and memory
    restart [NAME ...]     restart services, which reread their config and secrets
                           (default: proxy, gateway, api, worker)
    start | stop           start or stop the VM (the app starts with it in 2 to 3 minutes)
    logs                   Cloud Logging: --session ID, --service NAME, --errors, --since 1h
    backup | backups       a pg_dump now | the dumps in the bucket (kept 7 days)
    restore OBJECT         restore a dump (the app is stopped meanwhile)
    allow [CIDR ...]       replace the port 80 allow-list (--me adds this machine; --show;
                           --any opens it to every address: no TLS, so think first)
    secrets status         which secrets have a value
    secrets set NAME       put a value in (hidden prompt), e.g. openai-api-key
    secrets generate       database passwords and the uploads HMAC key, if still empty
                           (--lift-key-policy: see infra/gcp/README.md)
    ssh                    a shell on the VM (sudo for root)
    latency                round trips from the VM to OpenAI and Deepgram
    desktop-config         write the stage's URLs (terraform output desktop) into
                           apps/desktop/release.json, which release builds embed
"""

import argparse
import base64
import getpass
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# infra/gcp/envs/stage0/terraform.tfvars; override with the environment for another stage.
PROJECT = os.environ.get("COPILOT_GCP_PROJECT", "career-pilot-512115")
ZONE = os.environ.get("COPILOT_GCP_ZONE", "us-central1-a")
NAME = os.environ.get("COPILOT_GCP_NAME", "copilot-stage0")
REGION = ZONE.rsplit("-", 1)[0]
REGISTRY = f"{REGION}-docker.pkg.dev/{PROJECT}/copilot"
FIREWALL = f"{NAME}-http-allowlist"
BACKUPS = f"{NAME}-backups-{PROJECT}"
UPLOADS_SA = f"{NAME}-uploads@{PROJECT}.iam.gserviceaccount.com"
SECRETS = (
    "openai-api-key",
    "deepgram-api-key",
    "google-oauth-client-secret",
    "db-postgres",
    "db-app-api",
    "db-app-gateway",
    "db-app-worker",
    "db-app-system",
    "uploads-hmac-access-id",
    "uploads-hmac-secret",
)
GENERATED_DB = ("db-postgres", "db-app-api", "db-app-gateway", "db-app-worker", "db-app-system")
IMAGES = ("gateway", "api", "worker", "proxy")
ROOT = Path(__file__).resolve().parents[3]
# In GitHub Actions: no known host to check against on a fresh runner, and the SSH key gcloud
# makes for the deploy account's OS Login profile expires after the run.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def gcloud_bin() -> str:
    found = shutil.which("gcloud")
    if not found:
        sys.exit("gcloud is not installed: winget install Google.CloudSDK")
    return found


def gcloud(
    *args: str, capture: bool = False, stdin: bytes | None = None, check: bool = True
) -> str:
    cmd = [gcloud_bin(), *args, f"--project={PROJECT}"]
    result = subprocess.run(
        cmd,
        input=stdin,
        stdout=subprocess.PIPE if capture else None,
        # A captured call's warnings (e.g. a filter over no versions) are noise.
        stderr=subprocess.DEVNULL if capture and not check else None,
        check=False,
    )
    if check and result.returncode != 0:
        sys.exit(
            f"gcloud {args[0]} {args[1] if len(args) > 1 else ''} failed ({result.returncode})"
        )
    return result.stdout.decode() if capture and result.stdout else ""


def ssh_args(command: str) -> list[str]:
    args = ["compute", "ssh", NAME, f"--zone={ZONE}", "--tunnel-through-iap", "--quiet"]
    if IN_CI:
        args += ["--strict-host-key-checking=no", "--ssh-key-expire-after=2h"]
    if command:
        # On Windows gcloud is a .cmd script, and cmd.exe mangles newlines and some
        # characters on its command line; the script travels base64-encoded instead.
        encoded = base64.b64encode(command.encode()).decode()
        args.append(f"--command=echo {encoded} | base64 -d | bash")
    return args


def on_vm(command: str) -> None:
    gcloud(*ssh_args(command))


def on_vm_rc(command: str, capture: bool = False) -> tuple[int, str]:
    """Run on the VM without exiting on failure: (exit code, stdout if captured)."""
    cmd = [gcloud_bin(), *ssh_args(command), f"--project={PROJECT}"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None, check=False)
    return result.returncode, (result.stdout or b"").decode()


CTL = "sudo bash /var/lib/copilot/copilotctl.sh"


def ctl(*args: str) -> None:
    on_vm(f"{CTL} {' '.join(args)}")


# --- Deploys ---------------------------------------------------------------------------------


def main_sha() -> str:
    subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True)
    out = subprocess.run(["git", "rev-parse", "origin/main"], check=True, capture_output=True)
    return out.stdout.decode().strip()


def check_images(tag: str) -> None:
    for image in IMAGES:
        found = gcloud(
            "artifacts", "docker", "images", "describe", f"{REGISTRY}/{image}:{tag}",
            "--format=value(image_summary.digest)", capture=True, check=False,
        )  # fmt: skip
        if not found.strip():
            sys.exit(
                f"{REGISTRY}/{image}:{tag} is not in the registry; CI builds it on merge to main "
                "(.github/workflows/deploy.yml)"
            )


def deploy(tag: str | None, start: bool, check: bool) -> None:
    """copilotctl deploy, which runs the migrations and replaces each service in turn (it
    puts the previous tag back itself if the migrations fail or a service does not get
    healthy). With `check`, `health` runs after it; if that fails the previous tag is
    deployed again. Either failure exits non-zero, so the workflow fails."""
    tag = tag or main_sha()
    check_images(tag)
    notes: list[str] = []
    if ensure_running(start):
        notes.append(f"The VM `{NAME}` was stopped: started it for this deploy, left running.")
    before = deployed_tag()
    print(f"deploying {tag} (was {before})")
    code, _ = on_vm_rc(f"{CTL} deploy {tag}")
    if code != 0:
        healthy = health(quiet=True) if check else None
        summary(
            "failed",
            tag,
            before,
            [
                *notes,
                f"`copilotctl deploy {tag}` failed (migrations, or a service did not get "
                f"healthy); it put `{before}` back itself.",
                f"`{deployed_tag()}` is deployed now; health check: {verdict(healthy)}.",
            ],
        )
        sys.exit(f"deploy of {tag} failed; {before} is back")
    if not check:
        summary("deployed", tag, before, notes)
        return
    if health():
        public_check(notes)
        summary("deployed", tag, before, [*notes, "Health check passed: gateway and API."])
        return
    if before in ("none", "unknown", tag):
        summary(
            "failed",
            tag,
            before,
            [*notes, "The health check failed and there is no earlier tag to roll back to."],
        )
        sys.exit(f"health check failed on {tag}; nothing to roll back to")
    print(f"health check failed on {tag}: rolling back to {before}")
    code, _ = on_vm_rc(f"{CTL} rollback")
    healthy = code == 0 and health()
    summary(
        "rolled back",
        tag,
        before,
        [
            *notes,
            f"The health check failed on `{tag}`, so `{before}` was deployed again (rollback "
            f"{'succeeded' if code == 0 else 'FAILED'}; health check: {verdict(healthy)}).",
            f"`{deployed_tag()}` is deployed now.",
        ],
    )
    sys.exit(f"health check failed on {tag}; rolled back to {before}")


def verdict(ok: bool | None) -> str:
    return "not run" if ok is None else ("passed" if ok else "FAILED")


def vm_status() -> str:
    return gcloud(
        "compute", "instances", "describe", NAME, f"--zone={ZONE}", "--format=value(status)",
        capture=True,
    ).strip()  # fmt: skip


def ensure_running(start: bool) -> bool:
    """True if the VM was not running and was started (or resumed) here."""
    status = vm_status()
    if status == "RUNNING":
        return False
    if not start:
        sys.exit(f"the VM is {status}: `stage.py start` first, or deploy with --start")
    for _ in range(60):  # a stop in progress finishes first
        if status not in ("STOPPING", "SUSPENDING", "PROVISIONING", "STAGING"):
            break
        time.sleep(5)
        status = vm_status()
    if status == "SUSPENDED":
        print("the VM is suspended: resuming it")
        gcloud("compute", "instances", "resume", NAME, f"--zone={ZONE}")
    elif status != "RUNNING":
        print(f"the VM is {status}: starting it (it stays running after the deploy)")
        gcloud("compute", "instances", "start", NAME, f"--zone={ZONE}")
    # SSH comes up first; then cloud-init writes copilotctl and boots the data services.
    ready = (
        "sudo cloud-init status --wait >/dev/null 2>&1; "
        "test -f /var/lib/copilot/copilotctl.sh && systemctl is-active -q copilot-postgres"
    )
    for _ in range(40):
        if on_vm_rc(ready)[0] == 0:
            print("the VM is up")
            return True
        time.sleep(10)
    sys.exit("the VM started but its services did not come up in 7 minutes (stage.py status)")


def deployed_tag() -> str:
    code, out = on_vm_rc(
        "cat /mnt/disks/data/copilot/image-tag 2>/dev/null || echo none", capture=True
    )
    lines = out.strip().splitlines()
    return lines[-1].strip() if code == 0 and lines else "unknown"


# The gateway and the API as the app reaches them, minus the VPC firewall: each one's own
# /healthz, then through nginx the API's sign-in settings and the gateway's WebSocket
# handshake, which must end in the gateway's own `unauthorized` (no token is sent).
HEALTH = r"""set -u
api_direct() { curl -sf -m 5 -o /dev/null http://127.0.0.1:8080/healthz; }
gateway_direct() { curl -sf -m 5 -o /dev/null http://127.0.0.1:8081/healthz; }
api_proxy() { curl -sf -m 5 http://127.0.0.1/v1/auth/config | grep -q '"provider"'; }
gateway_proxy() {
  curl -s -m 5 -i -N -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    http://127.0.0.1/v1/session 2>/dev/null | grep -aq '"code":"unauthorized"'
}
for _ in $(seq 1 20); do
  failed=""
  for c in api_direct gateway_direct api_proxy gateway_proxy; do $c || failed="$failed $c"; done
  if [ -z "$failed" ]; then
    tag=$(cat /mnt/disks/data/copilot/image-tag 2>/dev/null)
    echo "health ok on $tag: api and gateway /healthz; through nginx /v1/auth/config"
    echo "and the gateway's reply to a /v1/session handshake"
    exit 0
  fi
  sleep 3
done
echo "health FAILED:$failed"
exit 1"""


def health(quiet: bool = False) -> bool:
    code, out = on_vm_rc(HEALTH, capture=True)
    if not quiet or code != 0:
        print(out.strip())
    return code == 0


def public_check(notes: list[str]) -> None:
    """The API check from this machine, through the VM's IP and its firewall. A warning only:
    port 80 may be limited to the team's addresses (stage.py allow)."""
    ip = gcloud(
        "compute", "instances", "describe", NAME, f"--zone={ZONE}",
        "--format=value(networkInterfaces[0].accessConfigs[0].natIP)", capture=True,
    ).strip()  # fmt: skip
    try:
        with urllib.request.urlopen(f"http://{ip}/v1/auth/config", timeout=10) as r:
            ok = r.status == 200 and b"provider" in r.read()
    except OSError:
        ok = False
    if ok:
        notes.append(f"`http://{ip}/v1/auth/config` answers from the runner too.")
    else:
        notes.append(
            f"`http://{ip}` is not reachable from the runner; expected if port 80 is limited "
            "to the team (stage.py allow)."
        )
        print(f"warning: http://{ip}/v1/auth/config not reachable from this machine")


def summary(outcome: str, tag: str, before: str, notes: list[str]) -> None:
    """Printed, and in GitHub Actions also the run's summary page."""
    lines = [f"### Stage 0 deploy: {outcome}", "", f"- Commit `{tag}` (was `{before}`)"]
    lines += [f"- {n}" for n in notes]
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


# --- Desktop release config ------------------------------------------------------------------


def desktop_config() -> None:
    """apps/desktop/release.json's URLs from `terraform output desktop` (the stage and the
    releases bucket). The rest of the file (the name shown, the updater's public key) stays."""
    tf = shutil.which("terraform") or sys.exit("terraform is not installed")
    out = subprocess.run(
        [tf, f"-chdir={ROOT / 'infra/gcp/envs/stage0'}", "output", "-json", "desktop"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    urls = json.loads(out)
    path = ROOT / "apps/desktop/release.json"
    cfg: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    keys = ("api_url", "gateway_url", "update_url")
    changed = {k: urls[k] for k in keys if cfg.get(k) != urls[k]}
    cfg.update(changed)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    done = ", ".join(f"{k} = {v}" for k, v in changed.items()) or "already current"
    print(f"{path}: {done}")
    print(f"download link for the team: {urls['download_url']}")


# --- Logs ------------------------------------------------------------------------------------


def logs(session: str | None, service: str | None, errors: bool, since: str, limit: int) -> None:
    parts = ['resource.type="gce_instance"', f'logName="projects/{PROJECT}/logs/cos_containers"']
    if session:
        parts.append(f'jsonPayload.session_id="{session}"')
    if service:
        parts.append(f'jsonPayload.service="{service}"')
    if errors:
        parts.append("severity>=ERROR")
    raw = gcloud(
        "logging", "read", " AND ".join(parts), f"--freshness={since}", f"--limit={limit}",
        "--order=asc", "--format=json", capture=True,
    )  # fmt: skip
    for entry in json.loads(raw or "[]"):
        p: dict[str, Any] = entry.get("jsonPayload") or {}
        text = p.get("msg") or p.get("message") or entry.get("textPayload") or json.dumps(p)
        if p.get("path"):  # nginx
            text = f"{p.get('method')} {p.get('path')} {p.get('status')} {p.get('ms')}s"
        ids = " ".join(f"{k}={p[k]}" for k in ("session_id", "user_id", "job") if p.get(k))
        level = entry.get("severity", "")
        print(
            f"{entry['timestamp'][:23]} {level:7} {p.get('service', '?'):9} {text} {ids}".rstrip()
        )


# --- Allow-list ------------------------------------------------------------------------------


def my_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return r.read().decode().strip()


def allow(cidrs: list[str], me: bool, show: bool, any_ip: bool) -> None:
    current = gcloud(
        "compute", "firewall-rules", "describe", FIREWALL, "--format=value(sourceRanges)",
        capture=True,
    ).strip().replace(";", ",")  # fmt: skip
    if show or (not cidrs and not me and not any_ip):
        print(f"{FIREWALL}: {current or '(empty)'}")
        return
    if any_ip:
        gcloud("compute", "firewall-rules", "update", FIREWALL, "--source-ranges=0.0.0.0/0")
        print(f"{FIREWALL}: every address (was {current}). No TLS: the sign-in token and call text")
        print(
            "cross every network in the clear; only the Google sign-in allow-list keeps people out."
        )
        return
    wanted = list(cidrs)
    if me:
        wanted.append(f"{my_ip()}/32")
    for c in wanted:
        if c in ("0.0.0.0/0", "::/0") or "/" not in c:
            sys.exit(f"{c}: give CIDRs such as 203.0.113.7/32 (or --any, deliberately)")
    wanted = sorted(set(wanted))
    gcloud("compute", "firewall-rules", "update", FIREWALL, f"--source-ranges={','.join(wanted)}")
    print(f"{FIREWALL}: {', '.join(wanted)} (was {current or 'empty'})")


# --- Secrets ---------------------------------------------------------------------------------


def secret_id(name: str) -> str:
    return f"{NAME}-{name}"


def has_value(name: str) -> bool:
    out = gcloud(
        "secrets", "versions", "list", secret_id(name), "--filter=state=ENABLED",
        "--format=value(name)", "--limit=1", capture=True, check=False,
    )  # fmt: skip
    return bool(out.strip())


def put_secret(name: str, value: str) -> None:
    gcloud("secrets", "versions", "add", secret_id(name), "--data-file=-", stdin=value.encode())


def secrets_status() -> None:
    for name in SECRETS:
        print(f"{secret_id(name):45} {'set' if has_value(name) else 'EMPTY'}")


def secrets_set(name: str) -> None:
    if name not in SECRETS:
        sys.exit(f"unknown secret {name}; one of: {', '.join(SECRETS)}")
    value = getpass.getpass(f"{name} (hidden): ").strip()
    if not value:
        sys.exit("empty; nothing stored")
    put_secret(name, value)
    print(f"{secret_id(name)}: new version stored; `stage.py status` / restart picks it up")


def secrets_generate(lift_key_policy: bool) -> None:
    alphabet = string.ascii_letters + string.digits  # used inside database URLs
    for name in GENERATED_DB:
        if has_value(name):
            print(f"{secret_id(name)}: already set, kept (rotation: infra/gcp/README.md)")
            continue
        put_secret(name, "".join(secrets.choice(alphabet) for _ in range(32)))
        print(f"{secret_id(name)}: generated")
    if has_value("uploads-hmac-secret") and has_value("uploads-hmac-access-id"):
        print("uploads HMAC key: already set, kept")
        return
    key = create_hmac_key()
    if key is None:
        if not lift_key_policy:
            sys.exit(
                "the organization's iam.disableServiceAccountKeyCreation policy refused the "
                "HMAC key; rerun with --lift-key-policy to lift it for this project around the "
                'one call (infra/gcp/README.md, "The uploads HMAC key")'
            )
        key = with_key_policy_lifted(create_hmac_key)
    # The secret is shown once by the API: it goes from gcloud's output straight into Secret
    # Manager, never to the terminal or the Terraform state.
    put_secret("uploads-hmac-access-id", key["metadata"]["accessId"])
    put_secret("uploads-hmac-secret", key["secret"])
    print(f"uploads HMAC key: created for {UPLOADS_SA}")


def create_hmac_key() -> dict[str, Any] | None:
    """A new HMAC key for the uploads account, or None if an organization policy refused it."""
    result = subprocess.run(
        [
            gcloud_bin(),
            "storage",
            "hmac",
            "create",
            UPLOADS_SA,
            "--format=json",
            f"--project={PROJECT}",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    if b"disableServiceAccountKeyCreation" in result.stderr:
        return None
    sys.exit(f"gcloud storage hmac create failed: {result.stderr.decode().strip()[:300]}")


def with_key_policy_lifted(make: Any) -> Any:
    """Lift iam.disableServiceAccountKeyCreation for this project only, call `make`, then put
    the organization's policy back (keys made meanwhile keep working; the policy blocks only
    making new ones). Needs Organization Administrator, which may grant itself Org Policy
    Administrator for the few minutes this takes."""
    org = gcloud("projects", "describe", PROJECT, "--format=value(parent.id)", capture=True).strip()
    me = (
        subprocess.run([gcloud_bin(), "config", "get", "account"], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )
    role = ["--member", f"user:{me}", "--role", "roles/orgpolicy.policyAdmin", "--condition=None"]
    print(f"granting {me} Org Policy Administrator on organization {org} for this step")
    subprocess.run(
        [gcloud_bin(), "organizations", "add-iam-policy-binding", org, *role, "--quiet"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    policy = ["iam.disableServiceAccountKeyCreation", f"--project={PROJECT}"]
    try:
        for _ in range(30):  # IAM and policy changes take a minute or two to apply
            done = subprocess.run(
                [gcloud_bin(), "resource-manager", "org-policies", "disable-enforce", *policy],
                capture_output=True,
                check=False,
            )
            if done.returncode == 0:
                break
            time.sleep(10)
        else:
            sys.exit("could not lift the policy for the project (no Org Policy Administrator?)")
        print(f"lifted iam.disableServiceAccountKeyCreation for {PROJECT}")
        for _ in range(30):
            made = make()
            if made is not None:
                return made
            time.sleep(10)
        sys.exit("the policy change did not reach Cloud Storage in five minutes; rerun later")
    finally:
        subprocess.run(
            [gcloud_bin(), "resource-manager", "org-policies", "delete", *policy, "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        print(f"restored the organization's policy on {PROJECT} (enforced again)")
        subprocess.run(
            [gcloud_bin(), "organizations", "remove-iam-policy-binding", org, *role, "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        print(f"removed {me}'s Org Policy Administrator grant")


# --- Latency ---------------------------------------------------------------------------------

# Ten fresh connections to each provider; medians of the TCP round trip, the TLS handshake
# (connect to handshake done) and the first byte of a 401 (no key is sent).
PROBE = r"""median() { sort -n | sed -n 5p; }
fmt="%{remote_ip} %{time_connect} %{time_appconnect} %{time_starttransfer}\n"
for u in https://api.openai.com/v1/models https://api.deepgram.com/v1/projects; do
  runs=$(for i in $(seq 10); do curl -s -o /dev/null -w "$fmt" "$u"; done)
  ip=$(echo "$runs" | head -n 1 | cut -d' ' -f1)
  tcp=$(echo "$runs" | awk '{print $2*1000}' | median)
  tls=$(echo "$runs" | awk '{print ($3-$2)*1000}' | median)
  first=$(echo "$runs" | awk '{print $4*1000}' | median)
  echo "$u ($ip): tcp rtt ${tcp%.*} ms, tls ${tls%.*} ms, first byte ${first%.*} ms (medians of 10)"
done"""


def latency() -> None:
    on_vm(PROBE)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("deploy")
    p.add_argument("--tag")
    p.add_argument("--start", action="store_true", help="start the VM first if it is stopped")
    p.add_argument("--check", action="store_true", help="health check after; roll back on failure")
    for name in (
        "rollback", "status", "start", "stop", "backup", "backups", "ssh", "latency", "health",
        "desktop-config",
    ):  # fmt: skip
        sub.add_parser(name)
    p = sub.add_parser("restart")
    p.add_argument("names", nargs="*", help="proxy gateway api worker postgres valkey elasticmq")
    p = sub.add_parser("logs")
    p.add_argument("--session")
    p.add_argument(
        "--service", choices=["gateway", "api", "worker", "scheduler", "migrate", "proxy"]
    )
    p.add_argument("--errors", action="store_true")
    p.add_argument("--since", default="1h", help="1h, 30m, 2d")
    p.add_argument("--limit", type=int, default=500)
    p = sub.add_parser("restore")
    p.add_argument("object", help="postgres/copilot-<stamp>.dump (see `backups`)")
    p = sub.add_parser("allow")
    p.add_argument("cidrs", nargs="*")
    p.add_argument("--me", action="store_true", help="add this machine's public IP")
    p.add_argument("--show", action="store_true")
    p.add_argument("--any", action="store_true", help="open port 80 to every address")
    p = sub.add_parser("secrets")
    p.add_argument("action", choices=["status", "set", "generate"])
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--lift-key-policy",
        action="store_true",
        help="generate: lift the org's service-account-key policy for this project around the "
        "HMAC key's creation, then restore it",
    )
    a = parser.parse_args()

    match a.cmd:
        case "deploy":
            deploy(a.tag, a.start, a.check)
        case "health":
            if not health():
                sys.exit(1)
        case "desktop-config":
            desktop_config()
        case "rollback":
            ctl("rollback")
        case "status":
            ctl("status")
        case "restart":
            known = {*IMAGES, "postgres", "valkey", "elasticmq"}
            if unknown := set(a.names) - known:
                sys.exit(f"unknown service {', '.join(sorted(unknown))}; one of {sorted(known)}")
            ctl("restart", *(a.names or ["api", "worker", "gateway", "proxy"]))
        case "start":
            gcloud("compute", "instances", "start", NAME, f"--zone={ZONE}")
            print("starting; the services are up in 2 to 3 minutes (stage.py status)")
        case "stop":
            gcloud("compute", "instances", "stop", NAME, f"--zone={ZONE}")
        case "logs":
            logs(a.session, a.service, a.errors, a.since, a.limit)
        case "backup":
            ctl("backup")
        case "backups":
            gcloud("storage", "ls", "-l", f"gs://{BACKUPS}/postgres/")
        case "restore":
            ctl("restore", a.object)
        case "allow":
            allow(a.cidrs, a.me, a.show, a.any)
        case "secrets":
            if a.action == "status":
                secrets_status()
            elif a.action == "set":
                if not a.name:
                    sys.exit("secrets set NAME")
                secrets_set(a.name)
            else:
                secrets_generate(a.lift_key_policy)
        case "ssh":
            on_vm("")
        case "latency":
            latency()


if __name__ == "__main__":
    main()
