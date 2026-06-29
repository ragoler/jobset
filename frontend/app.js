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
  chart: document.getElementById("error-chart"),
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
// Convergence curve: accumulated {x: samples thrown, y: |estimate - π|} points,
// gathered straight from the /pi poll (no backend/metrics needed). Reset per run.
const errSeries = [];
// LIVE only: the browser talks to the feature's Gateway directly, and the global LB
// is PROGRAMMED minutes after the Deployment is ready. Until the data path actually
// serves, we show a "provisioning…" state instead of letting a click "Failed to fetch".
let dataReady = false;

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
    errSeries.length = 0;
    drawErrorChart(0);
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

  // Accumulate the convergence point. Guard against a backwards total (a restart
  // we haven't observed yet) and only append when samples have actually advanced.
  const x = Math.min(p.total, p.target || p.total);
  if (errSeries.length && x < errSeries[errSeries.length - 1].x) errSeries.length = 0;
  if (!errSeries.length || x > errSeries[errSeries.length - 1].x) {
    errSeries.push({ x, y: err });
  }
  drawErrorChart(p.target || x);
}

function fmtBig(n) {
  if (n >= 1e9) return `${+(n / 1e9).toFixed(n % 1e9 ? 1 : 0)}B`;
  if (n >= 1e6) return `${+(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M`;
  if (n >= 1e3) return `${+(n / 1e3).toFixed(n % 1e3 ? 1 : 0)}k`;
  return `${n}`;
}

// Hand-drawn line chart (no library, so the feature stays self-contained/offline).
// x = samples thrown (0 → target), y = |estimate − π| (auto-scaled).
function drawErrorChart(xMax) {
  const cv = els.chart;
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 300;
  const h = cv.clientHeight || 180;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const css = getComputedStyle(document.documentElement);
  const accent = (css.getPropertyValue("--accent-2") || "#36d1b7").trim();
  const grid = "rgba(255,255,255,0.12)";
  const muted = (css.getPropertyValue("--muted") || "#8a97c0").trim();

  const m = { l: 52, r: 12, t: 12, b: 24 };
  const pw = w - m.l - m.r;
  const ph = h - m.t - m.b;

  ctx.font = "10px ui-monospace, monospace";
  ctx.strokeStyle = grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(m.l, m.t);
  ctx.lineTo(m.l, m.t + ph);
  ctx.lineTo(m.l + pw, m.t + ph);
  ctx.stroke();

  if (errSeries.length < 2) {
    ctx.fillStyle = muted;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("collecting samples…", m.l + pw / 2, m.t + ph / 2);
    return;
  }

  const yMax = Math.max(...errSeries.map((d) => d.y)) * 1.1 || 1e-3;
  const X = (x) => m.l + (xMax ? x / xMax : 0) * pw;
  const Y = (y) => m.t + ph - (yMax ? y / yMax : 0) * ph;

  // y labels (0 and yMax)
  ctx.fillStyle = muted;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.fillText(yMax.toExponential(1), m.l - 6, Y(yMax));
  ctx.fillText("0", m.l - 6, Y(0));
  // x labels (0 and target)
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillText("0", X(0), m.t + ph + 5);
  ctx.fillText(fmtBig(xMax), X(xMax), m.t + ph + 5);

  // the convergence line
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.beginPath();
  errSeries.forEach((d, i) => {
    const px = X(d.x);
    const py = Y(d.y);
    i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
  });
  ctx.stroke();

  // current point marker
  const last = errSeries[errSeries.length - 1];
  ctx.fillStyle = accent;
  ctx.beginPath();
  ctx.arc(X(last.x), Y(last.y), 3, 0, Math.PI * 2);
  ctx.fill();
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

/* ---- data-plane readiness gate --------------------------------------- */
// Re-resolve the Gateway IP (it programs minutes after the Deployment is ready) and
// probe the data path itself. Sets dataReady. LIVE only; MOCK is handled separately.
async function refreshDataPlane() {
  const override = new URLSearchParams(location.search).get("api");
  try {
    const r = await fetch(`${HUB_BASE}/config`, { headers: hubHeaders() });
    if (r.ok) {
      const c = await r.json();
      cfg.mode = c.mode || "LIVE";
      if (cfg.mode === "MOCK") {
        dataReady = false;
        return;
      }
      // Empty when the Hub has no PROGRAMMED gateway yet (get_gateway_ip returns "").
      cfg.dataBase = override || (c.gateway_ip ? `http://${c.gateway_ip}` : "");
    } else {
      cfg.mode = "LIVE";
      cfg.dataBase = override || location.origin; // standalone (no Hub)
    }
  } catch (_) {
    cfg.mode = "LIVE";
    cfg.dataBase = override || location.origin;
  }
  if (!cfg.dataBase) {
    dataReady = false; // gateway IP not assigned yet
    return;
  }
  // An assigned IP can still be minutes from serving — probe the real path.
  try {
    const h = await fetch(`${cfg.dataBase}/healthz`, { headers: dataHeaders() });
    dataReady = h.ok;
  } catch (_) {
    dataReady = false;
  }
}

function renderProvisioning() {
  els.mode.textContent = cfg.mode;
  els.mode.className = "badge badge-live";
  els.launch.disabled = true;
  els.launch.textContent = "Provisioning…";
  els.clear.disabled = true;
  els.phase.textContent = cfg.dataBase
    ? "· provisioning the load balancer — this can take a few minutes…"
    : "· waiting for the gateway IP…";
}

/* ---- polling --------------------------------------------------------- */
async function poll() {
  // LIVE hits the Gateway directly — gate on it actually serving before we fetch or
  // enable anything, so the first click can't land on a not-yet-programmed gateway.
  if (cfg.mode !== "MOCK" && !dataReady) {
    await refreshDataPlane();
    if (!dataReady) {
      renderProvisioning();
      return;
    }
  }
  try {
    const [sr, pr] = await Promise.all([
      fetch(dataUrl("/status"), { headers: dataHeaders() }),
      fetch(dataUrl("/pi"), { headers: dataHeaders() }),
    ]);
    let s = null;
    let p = null;
    if (sr.ok) {
      s = await sr.json();
      const r = s.restarts || 0;
      // A restart means the old (killed) pods are gone and a fresh group is up —
      // drop the optimistic "Killing…" flags and start the convergence curve over.
      if (r > lastRestarts) {
        killing.clear();
        errSeries.length = 0;
      }
      lastRestarts = r;
      lastStatus = s;
      renderPods(s);
    }
    if (pr.ok) {
      p = await pr.json();
      renderPi(p);
    }
    updateControls(s, p);
  } catch (_) {
    /* transient */
  }
}

// Drive Launch/Clear from the live cluster state. The key rule: you cannot Launch
// while a JobSet exists — you must Clear first. This prevents relaunching ON TOP of
// a run that is still starting (which would churn Spot nodes and look broken). The
// Launch label reflects what the existing run is doing so it's clear WHY it's
// disabled, rather than looking stuck.
function updateControls(s, p) {
  if (cfg.mode === "MOCK") {
    els.launch.disabled = true;
    els.launch.textContent = "Launch JobSet";
    els.clear.disabled = true;
    return;
  }
  const exists = !!(s && s.exists);
  els.clear.disabled = !exists;
  if (!exists) {
    els.launch.disabled = false;
    els.launch.textContent = "Launch JobSet";
    return;
  }
  els.launch.disabled = true;
  const live = !!(p && p.available !== false && (p.total || 0) > 0);
  const converged = !!(p && p.converged);
  els.launch.textContent = converged
    ? "Done — Clear to run again"
    : live
      ? "Running… (Clear to restart)"
      : "Starting…";
}

function startPolling() {
  if (poller) return;
  poll();
  poller = setInterval(poll, 1500);
}

/* ---- actions --------------------------------------------------------- */
async function launch() {
  // Disable immediately; from here on updateControls() (driven by the poll) owns the
  // button state — it stays disabled until the JobSet is Cleared, so there is no
  // window where it looks clickable while the run is still starting.
  els.launch.disabled = true;
  els.launch.textContent = "Starting…";
  killing.clear();
  lastRestarts = 0;
  errSeries.length = 0;
  els.phase.textContent = "· creating JobSet…";
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
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `launch failed: ${r.status}`);
    }
    running = true;
    startPolling();
  } catch (e) {
    // Failed to create — let the next poll restore the correct button state
    // (no JobSet -> Launch re-enables).
    els.phase.textContent = `· ${e.message}`;
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
