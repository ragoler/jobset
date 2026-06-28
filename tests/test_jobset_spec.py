"""Unit tests for JobSet CR construction (no cluster; pure dict assembly)."""

import pytest

import jobset_spec


def _build(**kw):
    args = dict(
        name="pi-estimator",
        namespace="gke-showcase-jobset",
        image="reg/jobset-pi:tag",
        workers=4,
        total_samples=1_000_000,
    )
    args.update(kw)
    return jobset_spec.build_jobset(**args)


def test_apiversion_and_kind():
    js = _build()
    assert js["apiVersion"] == "jobset.x-k8s.io/v1alpha2"
    assert js["kind"] == "JobSet"
    assert js["metadata"]["namespace"] == "gke-showcase-jobset"


def test_leader_and_workers_replicatedjobs():
    js = _build(workers=6)
    rjobs = {rj["name"]: rj for rj in js["spec"]["replicatedJobs"]}
    assert set(rjobs) == {"leader", "workers"}
    # leader: a single pod.
    leader_spec = rjobs["leader"]["template"]["spec"]
    assert leader_spec["parallelism"] == 1 and leader_spec["completions"] == 1
    # workers: N parallel pods, all must complete.
    w_spec = rjobs["workers"]["template"]["spec"]
    assert w_spec["parallelism"] == 6 and w_spec["completions"] == 6


def test_failure_policy_restarts_whole_group():
    js = _build(max_restarts=5)
    fp = js["spec"]["failurePolicy"]
    assert fp["maxRestarts"] == 5
    assert fp["restartStrategy"] == "Recreate"


def test_failure_policy_max_restarts_positive_by_default():
    assert _build()["spec"]["failurePolicy"]["maxRestarts"] > 0


def test_dns_hostnames_enabled_for_pod_to_pod():
    net = _build()["spec"]["network"]
    assert net["enableDNSHostnames"] is True
    assert net["publishNotReadyAddresses"] is True


def test_leader_host_dns_format():
    assert jobset_spec.leader_host("pi-estimator") == "pi-estimator-leader-0-0.pi-estimator"


def test_workers_get_leader_host_and_target_env():
    js = _build(workers=3, total_samples=900)
    w = next(rj for rj in js["spec"]["replicatedJobs"] if rj["name"] == "workers")
    env = {e["name"]: e.get("value") for e in
           w["template"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["LEADER_HOST"] == "pi-estimator-leader-0-0.pi-estimator"
    assert env["TARGET_SAMPLES"] == "900"
    assert env["WORKER_COUNT"] == "3"


def test_only_workers_pin_to_spot_leader_stays_stable():
    """Workers run on the Spot jobset-cpu class; the leader (the coordinator that
    must stay up for the whole run) does NOT, so it lands on the stable on-demand
    pool and isn't randomly preempted into spurious whole-group restarts."""
    js = _build()
    by_name = {rj["name"]: rj["template"]["spec"]["template"]["spec"]
               for rj in js["spec"]["replicatedJobs"]}

    worker_pod = by_name["workers"]
    assert worker_pod["nodeSelector"]["cloud.google.com/compute-class"] == "jobset-cpu"
    assert worker_pod["restartPolicy"] == "Never"

    leader_pod = by_name["leader"]
    assert "nodeSelector" not in leader_pod, "leader must not be pinned to Spot"
    assert leader_pod["restartPolicy"] == "Never"


def test_roles_use_correct_command():
    js = _build()
    cmds = {
        rj["name"]: rj["template"]["spec"]["template"]["spec"]["containers"][0]["command"]
        for rj in js["spec"]["replicatedJobs"]
    }
    assert cmds["leader"] == ["python3", "leader.py"]
    assert cmds["workers"] == ["python3", "worker.py"]


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        _build(workers=0)
    with pytest.raises(ValueError):
        _build(total_samples=0)
