# JobSet π Estimator π

Coordinated multi-pod **Monte Carlo π estimation** on the **JobSet** API / GKE.
Pick a worker count and a sample budget, hit **Launch**, and watch a single
JobSet bring up a **leader** pod and **N worker** pods *together* (gang
scheduling). The workers run **real** Monte Carlo sampling on **real** Spot CPU
nodes and stream their partial counts to the leader, which aggregates them into a
**live π estimate** that converges before your eyes. Then hit **Kill a worker**
and watch JobSet's signature behavior: deleting one pod restarts the **whole
group**.

This is a [gke_all](https://github.com/ragoler/gke_all) showcase feature
(`feature.yaml`). It runs **standalone** and as a **Hub feature**. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

> **No mocking.** LIVE mode runs a real JobSet doing real compute on real nodes
> and reports real cluster state. MOCK mode (so the Hub playroom imports offline)
> returns honest *empty / not-connected* states — never fake pods or fake π.

## How it works

```
Browser ──/launch──▶ Controller (FastAPI) ──creates──▶ JobSet (jobset.x-k8s.io)
   ▲                      │  reads π from leader            ├── leader  (1 pod)
   │  /status /pi /kill   │  lists/deletes pods             └── workers (N pods)
   └──────────────────────┘                          workers ──partials──▶ leader
                                                      via stable JobSet pod DNS
```

- **Gang startup:** one JobSet, two replicatedJobs (`leader` ×1, `workers` ×N);
  the operator creates all child Jobs together.
- **Real compute:** each worker throws random darts at the unit square and counts
  those inside the quarter circle — `π ≈ 4 × inside / total`. Independent seeds
  per worker make summing partials valid.
- **Pod-to-pod DNS:** `network.enableDNSHostnames: true` makes JobSet publish a
  headless service; the leader is reachable at `<jobset>-leader-0-0.<jobset>`.
- **Whole-group restart:** `failurePolicy.maxRestarts > 0` with
  `restartStrategy: Recreate` — kill any pod and JobSet recreates all child Jobs.
- **Cheap compute:** leader/worker pods select a Spot CPU `ComputeClass`, so GKE
  Node Auto-Provisioning grows the cluster on demand and scales back to zero.

## Layout

| Path | Purpose |
|---|---|
| `feature.yaml` | Hub descriptor |
| `app/` | controller (FastAPI), `leader.py`, `worker.py`, `montecarlo.py`, `jobset_spec.py`, Dockerfile |
| `frontend/` | playroom: launch form, topology, live π, kill-a-worker |
| `hub_router.py` | thin Hub data-plane router + honest MOCK |
| `infra/` | per-namespace: controller, leader Service, RBAC, Gateway, HTTPRoute |
| `cluster/` | cluster-scoped: JobSet operator (pinned v0.12.0) + Spot CPU ComputeClass |
| `*.sh`, `.env` | standalone lifecycle (create cluster, build/deploy, verify) |

## One image, three roles

A single image (`jobset-pi`) runs all three roles; the **command** selects which:

| Role | Command | What it does |
|---|---|---|
| controller | `uvicorn controller:app` (default CMD) | creates the JobSet, reports status, reads π, kills workers |
| leader | `python3 leader.py` (JobSet pod spec) | HTTP aggregator of worker partials → live π |
| worker | `python3 worker.py` (JobSet pod spec) | real Monte Carlo sampling → POSTs partials to leader |

This guarantees the leader and workers run the *exact same* Monte Carlo code.

## Standalone quickstart

```bash
cp .env.example .env            # set PROJECT_ID, CLUSTER_NAME, ZONE, ...
./setup_infra.sh                # create GKE cluster + JobSet operator + ComputeClass
./deploy_app.sh                 # build/push image + apply infra/ + discover Gateway IP
./verify_setup.sh               # REAL smoke test: launch, assert Running, π>0, restart
```

Open the printed Gateway IP in a browser for the full playroom.

Teardown: `./setup_infra.sh --delete` (keep cluster) or `--delete-cluster`.

## Tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn pydantic kubernetes pyyaml pytest httpx requests
python3 -m pytest -q
```

- `test_montecarlo.py` — the **real** sampling math converges toward π (unit-tested compute).
- `test_jobset_spec.py` — JobSet CR shape (leader+workers, failurePolicy, DNS).
- `test_controller.py` — `/launch`, `/kill-worker`, `/pi` with the k8s client mocked.
- `test_mock_router.py` — MOCK mode returns honest empty state (no fabricated data).
- `test_manifests.py` — every `${VAR}` declared, names match `feature.yaml`, RBAC verbs.

## Hub mode

The Hub builds the image, applies `cluster/` once + `infra/` per deploy, serves
the playroom at `/jobset/`, and mounts `hub_router.py` at
`/api/features/jobset`. The same frontend works both ways: it probes
`/api/features/jobset/config` (Hub) and falls back to its own origin (standalone).

> **Hub-core note:** JobSet introduces a new CRD kind (`jobset.x-k8s.io/jobsets`).
> The Hub's admin ClusterRole (`showcase-admin-role` in `infra/main-app.yaml`)
> must gain an `apiGroups: ["jobset.x-k8s.io"]`, `resources: ["jobsets"]` rule or
> the per-deploy apply 403s. This is the one sanctioned Hub-core edit (see
> feature.md §5).
