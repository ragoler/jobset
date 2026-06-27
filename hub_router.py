"""Hub data-plane router for the JobSet π-estimation feature.

Mounted by the Hub at ``/api/features/jobset`` behind the admin JWT. Kept thin:

* **LIVE** — the browser talks to the controller directly via the Gateway IP
  (CORS) for the heavy data plane (launch / status / pi / kill-worker / clear).
  This router only resolves ``/config`` (the gateway IP) using the shared SDK.
* **MOCK** — no cluster exists. Per the project's NO-MOCKING rule, this router
  returns HONEST empty / "not connected" states: ``/config`` reports MOCK with no
  gateway IP, and the data-plane endpoints return clearly-empty payloads marked
  ``available: false`` with no fabricated pods, π values, nodes, or runtimes. The
  playroom imports and renders offline, but shows nothing is connected — it never
  invents demo data.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

# --------------------------------------------------------------------------- #
# Shared SDK — imported tolerantly so the router also loads standalone/in tests.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised inside the Hub container
    from showcase_admin.app import config, database, k8s_client
except Exception:  # standalone / unit tests
    config = None
    database = None
    k8s_client = None


def _mode() -> str:
    """Resolve the current mode dynamically (never cache at import).

    The Hub's test harness sets ``MODE=MOCK`` after import, so a value captured at
    import time would go stale. Prefer the live ``config.MODE`` when the Hub SDK is
    present, else the ``MODE`` env var (standalone / unit tests), default MOCK.
    """
    if config is not None:
        return getattr(config, "MODE", "MOCK")
    return os.environ.get("MODE", "MOCK").upper()


FEATURE = "jobset"
GATEWAY_NAME = "jobset-gw"

router = APIRouter()


# Honest "nothing is connected" payloads for MOCK mode. NEVER fabricate pods,
# π values, nodes, or runtimes — the demo only shows REAL cluster state.
_MOCK_NOTE = "MOCK mode: not connected to a cluster — launch the feature LIVE on GKE."


@router.get("/config")
async def config_endpoint() -> dict:
    if _mode() == "MOCK":
        return {"mode": "MOCK", "gateway_ip": None, "note": _MOCK_NOTE}

    # LIVE: resolve the feature's deployed namespace, then this feature's Gateway IP
    # so the browser can reach the controller's data plane directly (CORS).
    gateway_ip = None
    if database is not None and k8s_client is not None:
        db = next(database.get_db())
        try:
            ns = database.get_feature_namespace(db, FEATURE)
            gateway_ip = await k8s_client.get_gateway_ip(ns, GATEWAY_NAME)
        except Exception:
            gateway_ip = None
        finally:
            db.close()
    return {"mode": "LIVE", "gateway_ip": gateway_ip}


@router.get("/status")
def status() -> dict:
    """MOCK: honest empty state. (LIVE status comes from the Gateway IP.)"""
    return {
        "mode": "MOCK",
        "exists": False,
        "jobset": None,
        "restarts": None,
        "pods": [],
        "note": _MOCK_NOTE,
    }


@router.get("/pi")
def pi() -> dict:
    """MOCK: no real computation offline, so report no estimate (NOT a fake π)."""
    return {
        "mode": "MOCK",
        "available": False,
        "pi": None,
        "inside": 0,
        "total": 0,
        "progress": 0.0,
        "converged": False,
        "note": _MOCK_NOTE,
    }
