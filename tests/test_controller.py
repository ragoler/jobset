"""Controller unit tests with the Kubernetes client mocked.

The k8s API is mocked (tests may mock the client — only the *computation* must
never be mocked). We assert the controller builds a valid JobSet spec on /launch
and selects the right worker pod on /kill-worker.
"""

import sys
import types
from unittest import mock

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

# Stub the `kubernetes` package BEFORE importing the controller so import-time
# `from kubernetes import ...` inside helpers resolves (controller imports it lazily,
# but be defensive). The real client is never contacted; _k8s() is patched per test.
sys.modules.setdefault("kubernetes", types.ModuleType("kubernetes"))

import controller  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(controller.app)


def _fake_pod(name, role, phase="Running", node="spot-node-1"):
    pod = mock.MagicMock()
    pod.metadata.name = name
    pod.metadata.labels = {controller.LBL_RJOB: role, controller.LBL_JOBSET: "pi-estimator"}
    pod.spec.node_name = node
    pod.status.phase = phase
    pod.status.start_time = None
    return pod


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_launch_creates_valid_jobset(client):
    core, custom = mock.MagicMock(), mock.MagicMock()
    # delete of any prior CR raises (none exists) -> swallowed.
    custom.delete_namespaced_custom_object.side_effect = Exception("not found")
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/launch", json={"workers": 5, "total_samples": 50000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workers"] == 5
    # The created object is a real JobSet CR with leader+workers and failurePolicy.
    _, kwargs = custom.create_namespaced_custom_object.call_args, None
    created = custom.create_namespaced_custom_object.call_args[0][-1]
    assert created["kind"] == "JobSet"
    rjobs = {rj["name"] for rj in created["spec"]["replicatedJobs"]}
    assert rjobs == {"leader", "workers"}
    assert created["spec"]["failurePolicy"]["maxRestarts"] > 0


def test_kill_worker_deletes_a_worker_pod(client):
    core, custom = mock.MagicMock(), mock.MagicMock()
    pods = mock.MagicMock()
    pods.items = [
        _fake_pod("pi-estimator-workers-0-abc", "workers"),
        _fake_pod("pi-estimator-workers-1-def", "workers"),
    ]
    core.list_namespaced_pod.return_value = pods
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/kill-worker")
    assert r.status_code == 200
    assert r.json()["killed"] == "pi-estimator-workers-0-abc"
    core.delete_namespaced_pod.assert_called_once()
    # The label selector targets workers of this JobSet only.
    _, kwargs = core.list_namespaced_pod.call_args
    assert controller.WORKERS_JOB in kwargs["label_selector"]


def test_kill_worker_409_when_none(client):
    core, custom = mock.MagicMock(), mock.MagicMock()
    empty = mock.MagicMock()
    empty.items = []
    core.list_namespaced_pod.return_value = empty
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/kill-worker")
    assert r.status_code == 409


def test_pi_reports_unavailable_when_leader_unreachable(client):
    # No real leader -> honest empty (available: false), never a fabricated π.
    with mock.patch.object(controller.urllib.request, "urlopen",
                           side_effect=OSError("no route")):
        r = client.get("/pi")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["pi"] == 0.0 and body["total"] == 0


def test_status_reports_not_exists_when_no_jobset(client):
    core, custom = mock.MagicMock(), mock.MagicMock()
    custom.get_namespaced_custom_object.side_effect = Exception("not found")
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["exists"] is False
    assert r.json()["pods"] == []
