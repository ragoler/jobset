"""MODE=MOCK hub_router tests — the playroom imports offline AND mock mode returns
HONEST empty state (no fabricated pods, π values, nodes, or runtimes)."""

import os

import pytest

os.environ["MODE"] = "MOCK"

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import hub_router  # noqa: E402


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(hub_router.router, prefix="/api/features/jobset")
    return TestClient(app)


def test_config_mock_has_no_gateway(client):
    r = client.get("/api/features/jobset/config")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "MOCK"
    assert body["gateway_ip"] is None  # nothing to link to offline
    assert "not connected" in body["note"].lower()


def test_status_mock_is_honestly_empty(client):
    r = client.get("/api/features/jobset/status")
    assert r.status_code == 200
    body = r.json()
    # No fabricated pods / running JobSet.
    assert body["exists"] is False
    assert body["pods"] == []
    assert body["jobset"] is None


def test_pi_mock_is_unavailable_not_fake(client):
    r = client.get("/api/features/jobset/pi")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    # CRITICAL: no fabricated π value in MOCK.
    assert body["pi"] is None
    assert body["total"] == 0 and body["inside"] == 0
    assert body["converged"] is False
