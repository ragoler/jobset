"""FastAPI controller for the JobSet π-estimation demo.

This process is the demo's control + data plane (NOT a JobSet pod). It:

  * creates a JobSet CR (1 leader + N workers, failurePolicy.maxRestarts > 0),
  * reports per-pod status / node / start-time / elapsed runtime (k8s API),
  * proxies the live π estimate read from the leader pod (via the leader's
    in-namespace Service),
  * kills a worker pod on demand (deletes one pod) so the UI can show the WHOLE
    JobSet restarting,
  * clears/deletes the JobSet.

Data-plane only: the browser calls this directly via the Gateway IP, so CORS is
mandatory. The Hub's JWT-protected control plane lives in ``hub_router.py``.

Namespace-portable: nothing hardcodes "default"; the namespace is read from the
downward API (POD_NAMESPACE).
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jobset_spec import (
    JOBSET_API_VERSION,
    LEADER_PORT,
    WORKERS_JOB,
    build_jobset,
    leader_host,
)

# --------------------------------------------------------------------------- #
# Configuration (all namespace-portable; nothing hardcodes "default").
# --------------------------------------------------------------------------- #
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
JOBSET_NAME = os.environ.get("JOBSET_NAME", "pi-estimator")
JOBSET_IMAGE = os.environ.get(
    "JOBSET_IMAGE", "jobset-pi:latest"
)
JOBSET_GROUP = "jobset.x-k8s.io"
JOBSET_VERSION = "v1alpha2"
JOBSET_PLURAL = "jobsets"
# JobSet's own pod labels (jobset.sigs.k8s.io/ prefix; NOT the API group).
LBL_JOBSET = "jobset.sigs.k8s.io/jobset-name"
LBL_RJOB = "jobset.sigs.k8s.io/replicatedjob-name"

app = FastAPI(title="JobSet π Estimator Controller")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Serve the playroom UI ourselves so the feature is fully functional STANDALONE
# (the Hub serves the same UI at /<slug>/, but standalone there is no Hub). The
# UI calls its own API same-origin (its /api/features/jobset/config probe 404s
# here and it falls back to LIVE against this origin). Mirrors the Hub static
# layout (/static/features/jobset/...) so index.html's asset paths resolve in both.
_FRONTEND = pathlib.Path(__file__).resolve().parent / "frontend"
if _FRONTEND.is_dir():
    app.mount(
        "/static/features/jobset",
        StaticFiles(directory=str(_FRONTEND)),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(_FRONTEND / "index.html"))

    @app.middleware("http")
    async def _no_cache_ui(request, call_next):
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


# --------------------------------------------------------------------------- #
# Kubernetes client helpers
# --------------------------------------------------------------------------- #
def _k8s():
    """Return (CoreV1Api, CustomObjectsApi), loading in/out-of-cluster config."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api(), client.CustomObjectsApi()


def _leader_url(path: str) -> str:
    """In-namespace URL of the leader. The infra/ ships a leader Service that
    selects the JobSet leader pod, so the controller reaches it by service name
    (namespace-portable, no DNS-host parsing needed inside the cluster)."""
    return f"http://jobset-leader:{LEADER_PORT}{path}"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class LaunchRequest(BaseModel):
    workers: int = Field(default=4, ge=1, le=20)
    total_samples: int = Field(default=20_000_000, ge=10_000, le=2_000_000_000)
    max_restarts: int = Field(default=3, ge=1, le=10)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/launch")
def launch(req: LaunchRequest) -> dict:
    """Create the JobSet (replacing any prior one) and return its name."""
    _, custom = _k8s()
    body = build_jobset(
        name=JOBSET_NAME,
        namespace=POD_NAMESPACE,
        image=JOBSET_IMAGE,
        workers=req.workers,
        total_samples=req.total_samples,
        max_restarts=req.max_restarts,
    )
    # Replace any existing JobSet so a fresh launch starts clean.
    try:
        custom.delete_namespaced_custom_object(
            JOBSET_GROUP, JOBSET_VERSION, POD_NAMESPACE, JOBSET_PLURAL, JOBSET_NAME
        )
        import time as _t

        _t.sleep(2)  # let the operator tear down the old child Jobs
    except Exception:
        pass
    try:
        custom.create_namespaced_custom_object(
            JOBSET_GROUP, JOBSET_VERSION, POD_NAMESPACE, JOBSET_PLURAL, body
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"create JobSet failed: {exc}")
    return {
        "jobset": JOBSET_NAME,
        "workers": req.workers,
        "total_samples": req.total_samples,
        "leader_host": leader_host(JOBSET_NAME),
    }


@app.get("/status")
def status() -> dict:
    """Per-pod status (leader + workers): node, phase, start time, runtime."""
    import datetime

    core, custom = _k8s()
    exists = True
    restarts = None
    try:
        js = custom.get_namespaced_custom_object(
            JOBSET_GROUP, JOBSET_VERSION, POD_NAMESPACE, JOBSET_PLURAL, JOBSET_NAME
        )
        restarts = (js.get("status") or {}).get("restarts")
    except Exception:
        exists = False

    pods_out: list[dict] = []
    if exists:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            pods = core.list_namespaced_pod(
                POD_NAMESPACE, label_selector=f"{LBL_JOBSET}={JOBSET_NAME}"
            )
            for p in pods.items:
                labels = p.metadata.labels or {}
                start = p.status.start_time
                elapsed = round((now - start).total_seconds(), 1) if start else None
                pods_out.append(
                    {
                        "pod_name": p.metadata.name,
                        "role": labels.get(LBL_RJOB, "unknown"),
                        "node": p.spec.node_name,
                        "status": p.status.phase,
                        "start_time": start.isoformat() if start else None,
                        "elapsed_s": elapsed,
                    }
                )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"cannot list pods: {exc}")

    pods_out.sort(key=lambda d: (d["role"] != "leader", d["pod_name"]))
    return {
        "namespace": POD_NAMESPACE,
        "jobset": JOBSET_NAME,
        "exists": exists,
        "restarts": restarts,
        "pods": pods_out,
    }


@app.get("/pi")
def pi() -> dict:
    """Live π estimate, read straight from the leader pod (REAL computation)."""
    try:
        with urllib.request.urlopen(_leader_url("/pi"), timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        # Leader not up yet (JobSet still starting / restarting) — honest empty.
        return {
            "pi": 0.0,
            "inside": 0,
            "total": 0,
            "progress": 0.0,
            "converged": False,
            "available": False,
            "detail": f"leader not reachable yet: {exc}",
        }


@app.post("/kill-worker")
def kill_worker() -> dict:
    """Delete one worker pod to demonstrate the WHOLE JobSet restarting.

    JobSet's failurePolicy (restartStrategy: Recreate) recreates ALL child Jobs
    when any pod fails, so deleting a single worker triggers a full-group restart.
    """
    core, _ = _k8s()
    try:
        pods = core.list_namespaced_pod(
            POD_NAMESPACE,
            label_selector=f"{LBL_JOBSET}={JOBSET_NAME},{LBL_RJOB}={WORKERS_JOB}",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"cannot list workers: {exc}")
    running = [p for p in pods.items if p.status.phase in ("Running", "Pending")]
    if not running:
        raise HTTPException(status_code=409, detail="no worker pod to kill")
    victim = running[0]
    try:
        core.delete_namespaced_pod(victim.metadata.name, POD_NAMESPACE)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"delete failed: {exc}")
    return {"killed": victim.metadata.name, "note": "JobSet will restart the whole group"}


@app.delete("/clear")
def clear() -> dict:
    """Delete the JobSet (and, via owner refs, all its child Jobs/pods)."""
    _, custom = _k8s()
    try:
        custom.delete_namespaced_custom_object(
            JOBSET_GROUP, JOBSET_VERSION, POD_NAMESPACE, JOBSET_PLURAL, JOBSET_NAME
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"delete failed: {exc}")
    return {"cleared": JOBSET_NAME}
