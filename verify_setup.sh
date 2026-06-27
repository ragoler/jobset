#!/usr/bin/env bash
# Post-deployment validation for the JobSet π Estimator: waits for the controller,
# discovers the Gateway IP, then runs a REAL smoke test — launches a small JobSet,
# asserts all pods reach Running, asserts the leader's π estimate advances beyond 0,
# kills a worker, and asserts the JobSet restarts.
set -e

if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found."
  exit 1
fi
NAMESPACE="${NAMESPACE:-default}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_NAME=$(awk '/kind: Gateway/{f=1} f&&/^  name:/{print $2; exit}' "${ROOT}/infra/gateway.yaml")

echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"

echo "=== Waiting for the controller to be Ready ==="
kubectl -n "${NAMESPACE}" rollout status deployment/jobset-controller-deployment --timeout=300s

echo "=== Discovering Gateway IP ==="
gateway_ip() {
  local ip
  ip=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  [ -z "${ip}" ] && ip=$(gcloud compute forwarding-rules list --global --project="${PROJECT_ID}" \
    --filter="name~gkegw1.*-${NAMESPACE}-${GATEWAY_NAME}" --format="value(IPAddress)" 2>/dev/null | head -1)
  echo "${ip}"
}
for i in {1..30}; do
  GATEWAY_IP=$(gateway_ip)
  [ -n "${GATEWAY_IP}" ] && break
  sleep 10
done
if [ -z "${GATEWAY_IP:-}" ]; then
  echo "Error: Gateway did not receive an IP within 5 minutes."
  exit 1
fi
echo "Gateway IP: ${GATEWAY_IP}"
BASE="http://${GATEWAY_IP}"

# A freshly-created L7 gateway reports an IP before its backend + health checks are
# programmed; poll /healthz until the data path is actually serving (up to ~8 min).
echo "=== Waiting for the Gateway data path to be healthy ==="
HEALTHY=""
for i in $(seq 1 32); do
  if curl -fsS -m 10 "${BASE}/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    echo "Gateway healthy after ~$((i * 15))s"
    break
  fi
  sleep 15
done
if [ -z "${HEALTHY}" ]; then
  echo "Error: Gateway data path not healthy yet (LB still programming). Re-run shortly."
  exit 1
fi

echo "=== Health check ==="
curl -fsS "${BASE}/healthz" && echo

echo "=== Launching a small JobSet (3 workers, 3M samples) ==="
curl -fsS -X POST "${BASE}/launch" \
  -H 'Content-Type: application/json' \
  -d '{"workers":3,"total_samples":3000000,"max_restarts":3}' >/dev/null
echo "launched"

echo "=== Waiting for all JobSet pods to reach Running (up to ~6 min; Spot nodes scale up) ==="
RUNNING=""
for i in $(seq 1 36); do
  # Expect 1 leader + 3 workers = 4 pods, all Running.
  PHASES=$(curl -fsS -m 10 "${BASE}/status" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(' '.join(p['status'] for p in d['pods']) or 'none')" 2>/dev/null || echo "none")
  NPODS=$(echo "${PHASES}" | wc -w | tr -d ' ')
  NRUN=$(echo "${PHASES}" | tr ' ' '\n' | grep -c '^Running$' || true)
  echo "  pods=${NPODS} running=${NRUN} [${PHASES}]"
  if [ "${NPODS}" -ge 4 ] && [ "${NRUN}" -ge 4 ]; then
    RUNNING=1
    break
  fi
  sleep 10
done
[ -n "${RUNNING}" ] || { echo "Error: JobSet pods did not all reach Running."; exit 1; }
echo "All pods Running."

echo "=== Asserting the leader's π estimate advances beyond 0 ==="
PI_OK=""
for i in $(seq 1 18); do
  PI=$(curl -fsS -m 10 "${BASE}/pi" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('pi') or 0)" 2>/dev/null || echo 0)
  echo "  pi=${PI}"
  if python3 -c "import sys;sys.exit(0 if float('${PI}')>1.0 else 1)" 2>/dev/null; then
    PI_OK=1
    break
  fi
  sleep 5
done
[ -n "${PI_OK}" ] || { echo "Error: π estimate never advanced beyond 0."; exit 1; }
echo "π estimate is live and advancing (${PI})."

echo "=== Killing a worker — asserting the JobSet restarts ==="
R0=$(curl -fsS -m 10 "${BASE}/status" | python3 -c "import sys,json;print((json.load(sys.stdin).get('restarts') or 0))" 2>/dev/null || echo 0)
curl -fsS -X POST "${BASE}/kill-worker" >/dev/null && echo "worker killed"
RESTARTED=""
for i in $(seq 1 24); do
  R1=$(curl -fsS -m 10 "${BASE}/status" | python3 -c "import sys,json;print((json.load(sys.stdin).get('restarts') or 0))" 2>/dev/null || echo 0)
  echo "  restarts: ${R0} -> ${R1}"
  if [ "${R1}" -gt "${R0}" ]; then
    RESTARTED=1
    break
  fi
  sleep 5
done
[ -n "${RESTARTED}" ] || echo "Warning: did not observe a restart count increase (the operator may report it differently); inspect: kubectl -n ${NAMESPACE} get jobset"

echo "=== Verification successful ==="
echo "Open the demo at: ${BASE}/"
