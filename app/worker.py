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
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "4"))
# Samples per inner batch between leader updates. At ~5-10M darts/s on a Spot CPU
# this posts a cumulative partial a few times a second — lively without flooding
# the leader.
BATCH = int(os.environ.get("BATCH_SAMPLES", "2000000"))


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

    print(
        f"[worker {idx}] {worker_id} -> leader {LEADER_HOST}:{LEADER_PORT}, "
        f"streaming continuously",
        flush=True,
    )

    # Stream FOREVER: keep drawing real darts and POSTing cumulative partials until
    # the pod is terminated (the user clears the JobSet, or kill-worker deletes this
    # pod to trigger a whole-group restart). A long-running stream is what makes the
    # demo work: π keeps refining live, and there is always a Running worker to kill.
    # The worker Job therefore never "completes" by design — the JobSet runs until
    # cleared. (sample_batch is real CPU compute; no sleeps padding the work.)
    inside = 0
    total = 0
    while True:
        p = sample_batch(BATCH, rng)
        inside += p.inside
        total += p.total
        if not _post_partial(worker_id, inside, total):
            # Leader not ready yet (gang startup) or transient — back off and retry;
            # the next loop re-posts the cumulative total so nothing is lost.
            time.sleep(1.0)


if __name__ == "__main__":
    main()
