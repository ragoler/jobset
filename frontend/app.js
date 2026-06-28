/* JobSet π Estimator — playroom frontend.
 *
 * Two call surfaces (mirrors the ray feature):
 *   - control plane: `/api/features/jobset/...` (Hub, JWT). Used for /config and
 *     ALL calls when MODE=MOCK (which returns honest empty state — no fake data).
 *   - data plane: the Gateway IP directly (CORS, no auth). Used for launch /
 *     status / pi / kill-worker / clear when running LIVE.
 *
 * In MOCK there is no cluster, so the UI shows "not connected" and the live view
 * stays empty — it never invents pods or π values.
 */

const HUB_BASE = "/api/features/jobset";

const els = {
  mode: document.getElementById("mode-badge"),
  workers: document.getElementById("workers"),
  samples: document.getElementById("samples"),
  launch: document.getElementById("launch"),
  clear: document.getElementById("clear"),
  phase: document.getElementById("phase"),
  piValue: document.getElementById("pi-value"),
  piError: document.getElementById("pi-error"),
  progressBar: document.getElementById("progress-bar"),
  statSamples: document.getElementById("stat-samples"),
  statInside: document.getElementById("stat-inside"),
  statRestarts: document.getElementById("stat-restarts"),
  statElapsed: document.getElementById("stat-elapsed"),
  pods: document.getElementById("pods"),
  podsEmpty: document.getElementById("pods-empty"),
  podCount: document.getElementById("pod-count"),
};

const PI = Math.PI;
let cfg = { mode: "MOCK", dataBase: HUB_BASE };
let poller = null;
let running = false;
// Pods the user has clicked Kill on — kept in state so the "Killing…" feedback
// survives the 1.5s poll re-render (otherwise the button looks clickable again
// before the operator has reacted, inviting double-clicks). Cleared once the whole
// group restarts (restart count rises) or on a fresh launch.
const killing = new Set();
let lastStatus = null; // most recent /status payload, for re-rendering on kill
let lastRestarts = 0; // detects when a whole-group restart has completed

/* ---- auth + bases ----------------------------------------------------- */
function jwt() {
  return localStorage.getItem("admin_jwt") || "";
}
function hubHeaders() {
  const h = { "Content-Type": "application/json" };
  const t = jwt();
  if (t) h["Authorization"] = `Bearer ${t}`;
  return h;
}
// In MOCK everything flows through the Hub (JWT). In LIVE the data plane hits the
// Gateway IP with CORS and no auth.
function dataHeaders() {
  return cfg.mode === "MOCK" ? hubHeaders() : { "Content-Type": "application/json" };
}
function dataUrl(path) {
  return cfg.mode === "MOCK" ? `${HUB_BASE}${path}` : `${cfg.dataBase}${path}`;
}

/* ---- config / bootstrap ---------------------------------------------- */
async function loadConfig() {
  const override = new URLSearchParams(location.search).get("api");
  try {
    const r = await fetch(`${HUB_BASE}/config`, { headers: hubHeaders() });
    if (r.ok) {
      const c = await r.json();
      cfg.mode = c.mode || "LIVE";
      cfg.dataBase =
        cfg.mode === "MOCK"
          ? HUB_BASE
          : override || (c.gateway_ip ? `http://${c.gateway_ip}` : HUB_BASE);
      return;
    }
  } catch (_) {
    /* fall through to standalone */
  }
  // Standalone: no Hub. Talk to the controller directly.
  cfg.mode = "LIVE";
  cfg.dataBase = override || location.origin;
}

function applyConfigUI() {
  els.mode.textContent = cfg.mode;
  els.mode.className = "badge " + (cfg.mode === "MOCK" ? "badge-mock" : "badge-live");
  if (cfg.mode === "MOCK") {
    els.launch.disabled = true;
    els.launch.title = "MOCK mode is not connected to a cluster";
    els.phase.textContent = "· MOCK — not connected to a cluster";
  }
}

/* ---- rendering -------------------------------------------------------- */
function fmtPi(v) {
  return v && v > 0 ? v.toFixed(6) : "—";
}

function renderPi(p) {
  if (!p || p.available === false || !p.total) {
    els.piValue.textContent = "—";
    els.piError.textContent = p && p.note ? p.note : "";
    els.progressBar.style.width = "0%";
    els.statSamples.textContent = "0 / 0";
    els.statInside.textContent = "0";
    return;
  }
  els.piValue.textContent = fmtPi(p.pi);
  const err = Math.abs(p.pi - PI);
  els.piError.textContent = `error ${err.toExponential(2)} vs π`;
  els.progressBar.style.width = `${Math.round((p.progress || 0) * 100)}%`;
  els.statSamples.textContent = `${(p.total || 0).toLocaleString()} / ${(p.target || 0).toLocaleString()}`;
  els.statInside.textContent = (p.inside || 0).toLocaleString();
  els.statElapsed.textContent = `${(p.elapsed_s || 0).toFixed(1)} s`;
  els.phase.textContent = p.converged ? "· converged" : "· estimating…";
}

function renderPods(s) {
  const pods = (s && s.pods) || [];
  els.statRestarts.textContent = (s && s.restarts) || 0;
  if (!pods.length) {
    els.pods.innerHTML = "";
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.innerHTML = s && s.exists
      ? "JobSet exists — pods are starting (gang scheduling)…"
      : 'No JobSet running. Hit <b>Launch</b>.';
    els.pods.appendChild(empty);
    els.podCount.textContent = "";
    return;
  }
  els.podCount.textContent = `· ${pods.length} pod(s)`;
  els.pods.innerHTML = "";
  pods.forEach((p) => {
    const isLeader = p.role === "leader";
    const ok = p.status === "Running" || p.status === "Succeeded";
    const isKilling = killing.has(p.pod_name);
    // Only running/pending workers can be killed (the leader is never killable, and
    // a finished worker has no pod to delete).
    const killable =
      !isLeader && (p.status === "Running" || p.status === "Pending") && cfg.mode !== "MOCK";
    const el = document.createElement("div");
    el.className =
      `pod ${isLeader ? "leader" : "worker"} ${ok ? "" : "pending"} ${isKilling ? "killing" : ""}`;
    let killBtn = "";
    if (isKilling) {
      killBtn = `<button class="pod-kill" disabled>Killing…</button>`;
    } else if (killable) {
      killBtn =
        `<button class="pod-kill" data-pod="${p.pod_name}" ` +
        `title="Kill this worker — the whole JobSet restarts">Kill</button>`;
    }
    el.innerHTML =
      `<span class="dot"></span>` +
      `<div class="pmeta">` +
      `<span class="pname" title="${p.pod_name}">${p.pod_name}</span>` +
      `<span class="psub">${isKilling ? "killing — group restarting…" : p.role + " · " + (p.node || "scheduling…")}</span>` +
      `</div>` +
      `<div class="pright">` +
      `<span class="pstatus">${isKilling ? "Killing" : p.status || "?"}</span>` +
      `<span class="page">${p.elapsed_s != null ? p.elapsed_s.toFixed(0) + "s" : "—"}</span>` +
      `</div>` +
      killBtn;
    els.pods.appendChild(el);
  });
}

/* ---- polling --------------------------------------------------------- */
async function poll() {
  try {
    const [sr, pr] = await Promise.all([
      fetch(dataUrl("/status"), { headers: dataHeaders() }),
      fetch(dataUrl("/pi"), { headers: dataHeaders() }),
    ]);
    if (sr.ok) {
      const s = await sr.json();
      const r = s.restarts || 0;
      // A restart means the old (killed) pods are gone and a fresh group is up —
      // drop the optimistic "Killing…" flags.
      if (r > lastRestarts) killing.clear();
      lastRestarts = r;
      lastStatus = s;
      renderPods(s);
      els.clear.disabled = !s.exists || cfg.mode === "MOCK";
    }
    if (pr.ok) renderPi(await pr.json());
  } catch (_) {
    /* transient */
  }
}

function startPolling() {
  if (poller) return;
  poll();
  poller = setInterval(poll, 1500);
}

/* ---- actions --------------------------------------------------------- */
async function launch() {
  els.launch.disabled = true;
  killing.clear();
  lastRestarts = 0;
  els.phase.textContent = "· replacing any previous run, then creating JobSet…";
  try {
    const body = JSON.stringify({
      workers: parseInt(els.workers.value, 10),
      total_samples: parseInt(els.samples.value, 10),
    });
    const r = await fetch(dataUrl("/launch"), {
      method: "POST",
      headers: dataHeaders(),
      body,
    });
    if (!r.ok) throw new Error(`launch failed: ${r.status}`);
    running = true;
    startPolling();
  } catch (e) {
    els.phase.textContent = `· ${e.message}`;
  } finally {
    els.launch.disabled = cfg.mode === "MOCK";
  }
}

async function killWorker(podName) {
  // Optimistically flag the pod so it renders as "Killing…" immediately and stays
  // that way across polls — no double-clicking while the operator reacts.
  killing.add(podName);
  if (lastStatus) renderPods(lastStatus);
  els.phase.textContent =
    `· killed ${podName} — the JobSet operator now recreates the WHOLE group ` +
    `(takes a few seconds on Spot; the restart count will tick up)…`;
  try {
    const r = await fetch(dataUrl(`/kill-worker?pod=${encodeURIComponent(podName)}`), {
      method: "POST",
      headers: dataHeaders(),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `kill failed: ${r.status}`);
    }
  } catch (e) {
    // The kill didn't take — clear the flag so the user can try again.
    killing.delete(podName);
    if (lastStatus) renderPods(lastStatus);
    els.phase.textContent = `· ${e.message}`;
  }
}

async function clearJobset() {
  els.clear.disabled = true;
  try {
    await fetch(dataUrl("/clear"), { method: "DELETE", headers: dataHeaders() });
    running = false;
    els.phase.textContent = "· cleared";
  } catch (_) {
    /* ignore */
  }
}

/* ---- init ------------------------------------------------------------ */
els.launch.addEventListener("click", launch);
els.clear.addEventListener("click", clearJobset);
// The pod cards are re-rendered every poll, so use event delegation for their
// per-worker Kill buttons rather than per-element listeners.
els.pods.addEventListener("click", (e) => {
  const btn = e.target.closest(".pod-kill");
  if (!btn || cfg.mode === "MOCK") return;
  btn.disabled = true;
  killWorker(btn.dataset.pod);
});

(async function init() {
  await loadConfig();
  applyConfigUI();
  // Poll regardless of mode so the live view reflects real cluster state (LIVE)
  // or stays honestly empty (MOCK).
  startPolling();
})();
