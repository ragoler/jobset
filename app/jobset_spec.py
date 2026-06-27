"""Builds the JobSet custom resource (jobset.x-k8s.io/v1alpha2) for the demo.

Kept separate from controller.py so it can be unit-tested without a live cluster
(the controller mocks the k8s client; this just constructs a dict).

The JobSet has two replicatedJobs:
  * **leader** — 1 pod, runs ``leader.py`` (the aggregator HTTP endpoint).
  * **workers** — N pods, run ``worker.py`` (real Monte Carlo sampling), each
    POSTing partials to the leader.

Coordinated/gang startup is inherent to JobSet: the operator creates all child
Jobs together. ``failurePolicy.maxRestarts`` gives JobSet's signature behavior —
if ANY pod fails, the WHOLE JobSet is recreated (restartStrategy: Recreate).

Pod-to-pod DNS: ``network.enableDNSHostnames: true`` makes the operator publish a
headless service named after the JobSet (the default ``subdomain``), so the leader
is reachable from every worker at ``<jobset>-leader-0-0.<jobset>``.
"""

from __future__ import annotations

JOBSET_API_VERSION = "jobset.x-k8s.io/v1alpha2"
LEADER_JOB = "leader"
WORKERS_JOB = "workers"
LEADER_PORT = 9000


def leader_host(jobset_name: str) -> str:
    """Stable DNS hostname of the (single) leader pod within the JobSet subdomain.

    JobSet pod FQDN: ``<jobSetName>-<replicatedJobName>-<jobIndex>-<podIndex>``;
    the leader replicatedJob has one job (index 0) with one pod (index 0). The
    subdomain defaults to the JobSet name.
    """
    return f"{jobset_name}-{LEADER_JOB}-0-0.{jobset_name}"


def _pod_spec(role: str, image: str, args_env: dict[str, str], cpu: str) -> dict:
    """A pod template for one role (leader/worker), pinned to the Spot CPU class."""
    env = [{"name": k, "value": v} for k, v in args_env.items()]
    # Expose the worker's JobSet job index to the container via the downward API
    # is not directly available; JobSet/Job inject JOB_COMPLETION_INDEX. We pass
    # the pod name through for logging/attribution.
    env.append(
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}
    )
    return {
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"cloud.google.com/compute-class": "jobset-cpu"},
            "containers": [
                {
                    "name": role,
                    "image": image,
                    "imagePullPolicy": "Always",
                    "command": ["python3", f"{role}.py"],
                    "env": env,
                    "resources": {
                        "requests": {"cpu": cpu, "memory": "256Mi"},
                        "limits": {"cpu": cpu, "memory": "512Mi"},
                    },
                    **({"ports": [{"containerPort": LEADER_PORT}]} if role == "leader" else {}),
                }
            ],
        }
    }


def build_jobset(
    *,
    name: str,
    namespace: str,
    image: str,
    workers: int,
    total_samples: int,
    max_restarts: int = 3,
) -> dict:
    """Construct the full JobSet CR dict.

    ``workers`` worker pods + 1 leader pod, all gang-started. ``max_restarts``
    must be > 0 so a killed worker triggers a whole-group restart (the demo's
    headline behavior).
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if total_samples < 1:
        raise ValueError("total_samples must be >= 1")

    lhost = leader_host(name)
    common_env = {
        "LEADER_HOST": lhost,
        "LEADER_PORT": str(LEADER_PORT),
        "TARGET_SAMPLES": str(total_samples),
        "WORKER_COUNT": str(workers),
    }

    return {
        "apiVersion": JOBSET_API_VERSION,
        "kind": "JobSet",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            # Whole-JobSet restart on ANY pod failure — JobSet's signature behavior.
            "failurePolicy": {
                "maxRestarts": max_restarts,
                "restartStrategy": "Recreate",
            },
            # Stable pod DNS via a headless service the operator creates (named
            # after the JobSet). Publish addresses before pods are Ready so a
            # worker can resolve the leader during the coordinated startup.
            "network": {
                "enableDNSHostnames": True,
                "publishNotReadyAddresses": True,
            },
            "replicatedJobs": [
                {
                    "name": LEADER_JOB,
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "parallelism": 1,
                            "completions": 1,
                            "backoffLimit": 0,
                            "template": _pod_spec(
                                "leader", image,
                                {"LEADER_PORT": str(LEADER_PORT),
                                 "TARGET_SAMPLES": str(total_samples)},
                                cpu="250m",
                            ),
                        }
                    },
                },
                {
                    "name": WORKERS_JOB,
                    "replicas": 1,
                    "template": {
                        "spec": {
                            # N parallel worker pods, all must complete.
                            "parallelism": workers,
                            "completions": workers,
                            "backoffLimit": 0,
                            "template": _pod_spec("worker", image, common_env, cpu="500m"),
                        }
                    },
                },
            ],
        },
    }
