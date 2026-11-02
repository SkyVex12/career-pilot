"""The wake function (Stage 0 on GCP, infra/gcp/README.md "Running on demand").

The VM runs only while someone uses it: it stops itself when idle (copilotctl idle), and the
desktop app starts it through this function, which also tells the app the VM's address (an
ephemeral IP, new at every start).

    GET  /   {"status": "TERMINATED", "host": null}: the VM's state and, when running, address
    POST /   the same, after starting the VM if it is stopped

Anyone may call it: the app has nothing to sign in with until the VM is up. It can only start
this one VM, which stops again after its idle time, so the worst misuse is the VM running for
a while; the billing budget's alerts catch that.
"""

import os
from typing import Any

import functions_framework
import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT = os.environ.get("COPILOT_PROJECT", "")
ZONE = os.environ.get("COPILOT_ZONE", "")
INSTANCE = os.environ.get("COPILOT_INSTANCE", "")
BASE = f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{INSTANCE}"

# Compute Engine states (instances.get) and what POST does in each.
STOPPED = {"TERMINATED", "STOPPED"}
SUSPENDED = {"SUSPENDED"}
HEADERS = {"Cache-Control": "no-store", "Content-Type": "application/json"}

_session: AuthorizedSession | None = None


def session() -> AuthorizedSession:
    global _session
    if _session is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        _session = AuthorizedSession(credentials)
    return _session


def address(vm: dict[str, Any]) -> str | None:
    for nic in vm.get("networkInterfaces", []):
        for access in nic.get("accessConfigs", []):
            if access.get("natIP"):
                return str(access["natIP"])
    return None


def view(s: AuthorizedSession, wake: bool) -> dict[str, Any]:
    response = s.get(BASE, timeout=10)
    response.raise_for_status()
    vm = response.json()
    status = str(vm.get("status", "UNKNOWN"))
    if wake and status in STOPPED | SUSPENDED:
        action = "start" if status in STOPPED else "resume"
        started = s.post(f"{BASE}/{action}", timeout=10)
        # 400 while it is still stopping: the app asks again in a few seconds.
        if started.ok:
            status = "STAGING"
    return {"status": status, "host": address(vm) if status == "RUNNING" else None}


@functions_framework.http
def wake(request: Any) -> tuple[dict[str, Any], int, dict[str, str]]:
    if request.method not in ("GET", "POST"):
        return {"error": "GET or POST"}, 405, HEADERS
    try:
        return view(session(), wake=request.method == "POST"), 200, HEADERS
    except Exception as e:
        return {"error": f"could not reach the VM: {type(e).__name__}"}, 502, HEADERS
