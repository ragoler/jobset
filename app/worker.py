"""JobSet WORKER role — REAL Monte Carlo sampling, streamed to the leader.

N worker pods per JobSet. Each worker runs the real ``montecarlo.sample_batch``
loop on real CPU, accumulating (inside, total) counts, and periodically POSTs its
*cumulative* partial to the leader. It addresses the leader by the stable JobSet
pod DNS name — the JobSet operator creates a headless service named after the
JobSet (its ``network.subdomain``), so every pod is reachable at:

    <jobSetName>-<replicatedJobName>-<jobIndex>-<podIndex>.<subdomain>

The leader is replicatedJob "leader" with a single pod, so its hostname is
``<jobSetName>-leader-0-0.<jobSetName>``. We resolve that from the env the
controller injects (LEADER_HOST), retrying until the leader is up (gang startup
means it is coming up alongside us).

Each worker seeds its RNG distinctly (from its JobSet job index) so the workers
sample independent streams — summing independent partials is what makes the
aggregate estimate valid.
"""

from __future__ import annotations

import os
import random
import socket
import time
import urllib.error
import urllib.request

from montecarlo import sample_batch

LEADER_HOST = os.environ.get("LEADER_HOST", "localhost")
LEADER_PORT = int(os.environ.get("LEADER_PORT", "9000"))
# How many darts the whole JobSet should draw, and how many workers split it.
TARGET_SAMPLES = int(os.environ.get("TARGET_SAMPLES", "20000000"))
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "4"))
# Samples per inner batch between leader updates (keeps the stream lively).
BATCH = int(os.environ.get("BATCH_SAMPLES", "50000"))


def _worker_index() -> int:
    """This worker's JobSet job index (0..WORKER_COUNT-1).

    JobSet injects JOB_COMPLETION_INDEX (the Job completion index) into each pod;
    we fall back to parsing the hostname (``...-workers-<idx>-0``) and finally 0.
    """
    for key in ("JOB_COMPLETION_INDEX", "JOBSET_JOB_INDEX"):
        v = os.environ.get(key)
        if v is not None and v.isdigit():
            return int(v)
    host = socket.gethostname()
    parts = host.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdigit():
        return int(parts[1])
    return 0


def _post_partial(worker_id: str, inside: int, total: int) -> bool:
    url = f"http://{LEADER_HOST}:{LEADER_PORT}/partial"
    data = (
        '{"worker_id": "%s", "inside": %d, "total": %d}' % (worker_id, inside, total)
    ).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def main() -> None:
    idx = _worker_index()
    worker_id = socket.gethostname()
    # Distinct, deterministic seed per worker -> independent sample streams.
    rng = random.Random(1000 + idx)
    # Split the global target evenly; the last worker mops up any remainder.
    per_worker = TARGET_SAMPLES // max(WORKER_COUNT, 1)
    if idx == WORKER_COUNT - 1:
        per_worker += TARGET_SAMPLES - per_worker * WORKER_COUNT

    print(
        f"[worker {idx}] {worker_id} -> leader {LEADER_HOST}:{LEADER_PORT}, "
        f"target={per_worker} darts",
        flush=True,
    )

    inside = 0
    total = 0
    # Wait for the leader to accept our first partial (gang startup: it is coming
    # up alongside us). Keep sampling while we wait so no CPU is wasted.
    while total < per_worker:
        n = min(BATCH, per_worker - total)
        p = sample_batch(n, rng)
        inside += p.inside
        total += p.total
        if not _post_partial(worker_id, inside, total):
            # Leader not ready / transient — back off briefly and retry next loop.
            time.sleep(1.0)

    # Final flush so the leader has our complete count even if the last POST raced.
    for _ in range(10):
        if _post_partial(worker_id, inside, total):
            break
        time.sleep(1.0)
    print(f"[worker {idx}] done: inside={inside} total={total}", flush=True)


if __name__ == "__main__":
    main()
