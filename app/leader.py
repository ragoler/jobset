"""JobSet LEADER role — aggregates worker partials into a live π estimate.

One leader pod per JobSet. It runs a tiny HTTP endpoint that workers POST their
partial (inside, total) counts to, over the stable JobSet pod DNS (a headless
service the JobSet operator creates). It maintains running totals and exposes the
current π = 4 * inside / total, plus per-worker progress, so the controller (and
through it the playroom) can show the estimate converging in real time.

Pure stdlib HTTP server (no FastAPI) to keep the leader lean — the heavy API
surface lives in the controller.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from montecarlo import estimate_pi

PORT = int(os.environ.get("LEADER_PORT", "9000"))
# Total samples the whole JobSet should draw (sum across all workers). The leader
# reports "converged" once the workers have collectively reached this many.
TARGET_SAMPLES = int(os.environ.get("TARGET_SAMPLES", "20000000"))

_LOCK = threading.Lock()
_STATE: dict = {
    "inside": 0,
    "total": 0,
    "target": TARGET_SAMPLES,
    "workers": {},  # worker_id -> {"inside", "total", "last_seen"}
    "started": time.time(),
}


def _snapshot() -> dict:
    with _LOCK:
        total = _STATE["total"]
        return {
            "pi": estimate_pi(_STATE["inside"], total),
            "inside": _STATE["inside"],
            "total": total,
            "target": _STATE["target"],
            "progress": min(1.0, total / _STATE["target"]) if _STATE["target"] else 0.0,
            "converged": _STATE["target"] > 0 and total >= _STATE["target"],
            "elapsed_s": round(time.time() - _STATE["started"], 1),
            "workers": {
                wid: {
                    "inside": w["inside"],
                    "total": w["total"],
                    "pi": estimate_pi(w["inside"], w["total"]),
                    "last_seen_s": round(time.time() - w["last_seen"], 1),
                }
                for wid, w in _STATE["workers"].items()
            },
        }


class Handler(BaseHTTPRequestHandler):
    # Silence the default noisy logging.
    def log_message(self, *args):  # noqa: D401
        return

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/healthz", "/"):
            self._send(200, {"status": "ok"})
        elif self.path in ("/pi", "/status"):
            self._send(200, _snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/partial":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            wid = str(body["worker_id"])
            inside = int(body["inside"])
            total = int(body["total"])
        except Exception as exc:  # malformed partial — report, don't crash
            self._send(400, {"error": f"bad partial: {exc}"})
            return

        with _LOCK:
            prev = _STATE["workers"].get(wid, {"inside": 0, "total": 0})
            # Each worker POSTs cumulative counts; apply the delta so a retried or
            # duplicated POST can't double-count the running totals.
            _STATE["inside"] += inside - prev["inside"]
            _STATE["total"] += total - prev["total"]
            _STATE["workers"][wid] = {
                "inside": inside,
                "total": total,
                "last_seen": time.time(),
            }
        self._send(200, {"ok": True})


def main() -> None:
    print(f"[leader] listening on :{PORT}, target_samples={TARGET_SAMPLES}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
