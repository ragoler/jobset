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


def _api_error(status):
    """A stand-in for kubernetes.client ApiException — the controller branches on
    the ``.status`` attribute (e.g. 404 == gone), so we only need that."""
    e = Exception(f"api error {status}")
    e.status = status
    return e


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
    # No prior CR: the delete 404s (swallowed) and the "is it gone?" poll 404s, so
    # the create proceeds immediately without waiting.
    custom.delete_namespaced_custom_object.side_effect = _api_error(404)
    custom.get_namespaced_custom_object.side_effect = _api_error(404)
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/launch", json={"workers": 5, "total_samples": 50000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workers"] == 5
    # The created object is a real JobSet CR with leader+workers and failurePolicy,
    # and the chosen worker count actually flows into the spec (regression: a new
    # config must take effect, not be ignored).
    created = custom.create_namespaced_custom_object.call_args[0][-1]
    assert created["kind"] == "JobSet"
    by_name = {rj["name"]: rj for rj in created["spec"]["replicatedJobs"]}
    assert set(by_name) == {"leader", "workers"}
    assert by_name["workers"]["template"]["spec"]["parallelism"] == 5
    assert by_name["workers"]["template"]["spec"]["completions"] == 5
    assert created["spec"]["failurePolicy"]["maxRestarts"] > 0


def test_launch_waits_for_old_jobset_to_terminate(client):
    """If the old JobSet lingers (still terminating), launch must NOT create over it
    and silently keep the old run — it returns 409 instead."""
    core, custom = mock.MagicMock(), mock.MagicMock()
    custom.delete_namespaced_custom_object.return_value = {}
    # The old object never disappears -> the existence poll never 404s.
    custom.get_namespaced_custom_object.return_value = {"metadata": {"name": "pi-estimator"}}
    # Patch time.sleep so the bounded poll doesn't actually wait the full ~60s.
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)), \
         mock.patch("time.sleep", return_value=None):
        r = client.post("/launch", json={"workers": 2, "total_samples": 50000})
    assert r.status_code == 409
    custom.create_namespaced_custom_object.assert_not_called()


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


def test_kill_worker_targets_a_named_pod(client):
    """?pod=<name> kills exactly that worker (the UI's per-pod button)."""
    core, custom = mock.MagicMock(), mock.MagicMock()
    pods = mock.MagicMock()
    pods.items = [
        _fake_pod("pi-estimator-workers-0-abc", "workers"),
        _fake_pod("pi-estimator-workers-1-def", "workers"),
    ]
    core.list_namespaced_pod.return_value = pods
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/kill-worker?pod=pi-estimator-workers-1-def")
    assert r.status_code == 200
    assert r.json()["killed"] == "pi-estimator-workers-1-def"
    name = core.delete_namespaced_pod.call_args[0][0]
    assert name == "pi-estimator-workers-1-def"


def test_kill_worker_404_for_unknown_pod(client):
    core, custom = mock.MagicMock(), mock.MagicMock()
    pods = mock.MagicMock()
    pods.items = [_fake_pod("pi-estimator-workers-0-abc", "workers")]
    core.list_namespaced_pod.return_value = pods
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/kill-worker?pod=does-not-exist")
    assert r.status_code == 404
    core.delete_namespaced_pod.assert_not_called()


def test_kill_worker_409_for_finished_pod(client):
    """A Succeeded worker has no pod to kill -> 409, not a silent no-op."""
    core, custom = mock.MagicMock(), mock.MagicMock()
    pods = mock.MagicMock()
    pods.items = [_fake_pod("pi-estimator-workers-0-abc", "workers", phase="Succeeded")]
    core.list_namespaced_pod.return_value = pods
    with mock.patch.object(controller, "_k8s", return_value=(core, custom)):
        r = client.post("/kill-worker?pod=pi-estimator-workers-0-abc")
    assert r.status_code == 409
    core.delete_namespaced_pod.assert_not_called()


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
