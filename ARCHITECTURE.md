# Architecture — how the JobSet π Estimator works (a learning guide)

This walks through the whole implementation so you can learn from it. The compute
is deliberately simple (Monte Carlo π) so the interesting part is the
**JobSet-on-GKE machinery** — coordinated startup, stable pod DNS, and
whole-group restart — not the math.

---

## 1. The one-sentence idea

A single **JobSet** brings up a **leader** pod and **N worker** pods *together*;
the workers do real Monte Carlo sampling and stream partial counts to the leader,
which aggregates them into a live π estimate — and if any pod dies, JobSet
restarts the **whole group**.

Monte Carlo π is an *embarrassingly parallel* stand-in for real coordinated batch
work (distributed training, MPI-style HPC, data processing fan-in).

---

## 2. The moving parts

```
Browser (playroom)                       ── served by the controller (standalone) or the Hub
  │  POST /launch, GET /status, GET /pi, POST /kill-worker, DELETE /clear
  ▼
Gateway (gke-l7-global-external-managed) ── dedicated L7 LB, one public IP
  │  HTTPRoute "/" → jobset-controller:80
  ▼
Controller  (Deployment, FastAPI)        ── creates the JobSet CR; reads the leader's π
  │  create JobSet (1 leader + N workers)
  ▼
JobSet  (jobset.x-k8s.io/v1alpha2)        ── reconciled by the JobSet operator
  ├── leader  Job  (1 pod)   leader.py — HTTP aggregator, exposes live π
  └── workers Job  (N pods)  worker.py — real Monte Carlo, POSTs partials
        │
        └── workers → leader over the stable JobSet pod DNS (headless service)
```

Repo map:

| Path | Role |
|---|---|
| `app/controller.py` | FastAPI control + data plane (creates JobSet, status, π, kill) |
| `app/leader.py` | stdlib HTTP aggregator: sums worker partials → live π |
| `app/worker.py` | real Monte Carlo loop → POSTs cumulative partials to the leader |
| `app/montecarlo.py` | the pure-stdlib sampling math (unit-tested) |
| `app/jobset_spec.py` | builds the JobSet CR (testable without a cluster) |
| `frontend/` | the playroom (launch form, topology, π readout, kill button) |
| `infra/` | per-namespace K8s: controller, leader Service, RBAC, Gateway/HTTPRoute |
| `cluster/` | cluster-scoped: JobSet operator (pinned) + Spot CPU ComputeClass |
| `hub_router.py` | thin Hub data-plane router + honest MOCK |
| `*.sh`, `.env` | standalone lifecycle (create cluster, build/deploy, verify) |

---

## 3. The data flow (the heart of it)

1. **Browser → `POST /launch`** with `{workers, total_samples}`.
2. **Controller builds a JobSet CR** (`jobset_spec.build_jobset`): two
   replicatedJobs — `leader` (1 pod) and `workers` (N pods) — with
   `failurePolicy.maxRestarts` and `network.enableDNSHostnames`.
3. **The JobSet operator creates all child Jobs together** (gang startup) and a
   headless service for pod DNS.
4. **Each worker** (`worker.py`) seeds its RNG from its JobSet job index, runs the
   real `montecarlo.sample_batch` loop, and periodically **POSTs its cumulative
   `(inside, total)`** to the leader at `<jobset>-leader-0-0.<jobset>:9000`.
5. **The leader** (`leader.py`) keeps running totals (applying each worker's POST
   as a delta, so a retried POST can't double-count) and computes
   `π = 4 × inside / total`.
6. **The controller** reads the leader's `/pi` through an in-namespace
   `jobset-leader` Service and re-exposes it at `/pi`; the playroom polls it and
   shows the estimate converging, plus a per-pod topology from `/status`.
7. **Kill a worker:** `POST /kill-worker` deletes one worker pod →
   JobSet's `restartStrategy: Recreate` recreates **all** child Jobs → the whole
   group restarts (the restart count climbs in the UI).

**Pod attribution:** each pod gets `POD_NAME` via the downward API; `/status`
lists every JobSet pod (selected by the `jobset.sigs.k8s.io/jobset-name` label)
with its node, phase, and elapsed runtime.

---

## 4. JobSet concepts you can take away

- **JobSet** groups several `Job`s (replicatedJobs) into one unit with coordinated
  lifecycle — created together, restarted together. Ideal for leader/worker or
  driver/worker topologies.
- **`replicas` vs `parallelism`:** here the `workers` replicatedJob uses one Job
  with `parallelism: N` (N pods, one role). A replicatedJob can also have
  `replicas > 1` to clone an entire Job (e.g. per-rack groups).
- **Stable pod DNS:** `network.enableDNSHostnames: true` publishes a headless
  service named after the JobSet; every pod is reachable at
  `<jobSetName>-<replicatedJobName>-<jobIndex>-<podIndex>.<subdomain>`. The single
  leader is `<jobset>-leader-0-0.<jobset>`. `publishNotReadyAddresses: true` lets a
  worker resolve the leader *during* startup, before it reports Ready.
- **`failurePolicy.maxRestarts` + `restartStrategy`:** a failure is recorded when
  any child Job fails; the JobSet restarts (recreates all Jobs) until the count
  reaches `maxRestarts`, then it is terminally failed. `Recreate` (default) tears
  down and recreates; `BlockingRecreate` waits for full teardown first.
- **Labels** use the `jobset.sigs.k8s.io/` prefix (e.g.
  `jobset.sigs.k8s.io/jobset-name`, `.../replicatedjob-name`), distinct from the
  CRD API group `jobset.x-k8s.io`.

---

## 5. GKE autoscaling (the GKE story)

- The **worker** pods select `cloud.google.com/compute-class: jobset-cpu`
  (`cluster/cpu-computeclass.yaml`), a Spot CPU ComputeClass with
  `nodePoolAutoCreation: enabled`. The **leader** is deliberately *not* pinned to
  Spot — it's the coordinator/aggregator that must stay up for the whole run, so it
  schedules on the cluster's stable on-demand pool. (Pinning the leader to Spot let
  random preemptions fail its Job and spuriously restart the whole JobSet.)
- When the worker pods can't schedule (no node), **GKE Node Auto-Provisioning**
  creates **Spot** CPU node pools on demand; once the workers finish their darts
  and complete, the empty Spot nodes scale back to zero. The leader (on the stable
  pool) keeps serving the final π until you `DELETE /clear` the JobSet.
- Each worker throws its even share of the **target** dart count, POSTing
  cumulative partials so π and the progress bar advance live, then exits 0 (its Job
  completes). Size the target so the run lasts long enough to watch and to hit
  "kill a worker" mid-run (the default is 1 billion darts ≈ 30-60s on a few Spot
  CPUs). A worker killed **before** it finishes fails its Job and triggers JobSet's
  whole-group restart.
- This is the value prop: *a batch group appears → cheap compute appears → group
  done → it disappears.*

---

## 6. Networking (and the gotchas)

- **Dedicated Gateway** (`gke-l7-global-external-managed`): a dedicated L7 LB per
  Gateway, so this feature doesn't collide with others on a shared cluster.
- **`HTTPRoute`** sends `/` to the controller Service (UI + API on one origin).
- **Leader Service (`jobset-leader`)**: the controller is *not* a JobSet pod, so it
  can't use the JobSet pod DNS directly; instead a stable in-namespace Service
  selects the leader pod by its JobSet labels, with `publishNotReadyAddresses` so
  the controller can read partials early. Worker→leader traffic uses the JobSet's
  own headless service (pod DNS).

---

## 7. Standalone *and* Hub (the feature contract)

The repo follows the [gke_all feature contract](https://github.com/ragoler/gke_all/blob/main/feature.md):

- `feature.yaml` is the descriptor the Hub reads (paths, gateway, build, router).
- **Standalone:** `setup_infra.sh` (create cluster + operator + ComputeClass) →
  `deploy_app.sh` (build image + apply `infra/`) → `verify_setup.sh` (real smoke
  test). The controller serves the playroom itself, so the Gateway IP shows the UI.
- **Hub:** the Hub builds the image, applies `cluster/` once + `infra/` per deploy,
  serves the playroom at `/jobset/`, and mounts `hub_router.py` at
  `/api/features/jobset`. The same frontend probes `/api/features/jobset/config`
  (Hub) and falls back to its own origin (standalone).
- **`MODE=MOCK`** makes `hub_router.py` return honest *empty / not-connected*
  states so the UI imports offline — it never fabricates pods or π values.

> **New CRD kind needs a Hub RBAC grant.** JobSet adds `jobset.x-k8s.io/jobsets`.
> Add an `apiGroups: ["jobset.x-k8s.io"], resources: ["jobsets"]` rule to the Hub
> admin ClusterRole (`showcase-admin-role` in `infra/main-app.yaml`) or the deploy
> 403s mid-apply (feature.md §5).

---

## 8. Bonus: queueing JobSets with Kueue (mention only)

For many concurrent JobSets contending for limited Spot capacity, **[Kueue](https://kueue.sigs.k8s.io/)**
is the natural next layer. Kueue natively understands JobSet as a workload kind:
you would label each JobSet with a `kueue.x-k8s.io/queue-name`, define a
`ClusterQueue` + `ResourceFlavor` capping CPU, and Kueue would **admit** JobSets
only when quota is free — gang-admitting all pods of a JobSet at once (so a
half-scheduled group never wastes Spot nodes) and queueing the rest. This demo
deliberately runs a single JobSet at a time and does **not** install Kueue; it is
noted here as the production scaling path, not implemented.
