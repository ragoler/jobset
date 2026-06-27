#!/usr/bin/env bash
# Standalone provisioning for the JobSet π Estimator: GKE cluster + cluster-scoped
# prerequisites (JobSet operator + Spot CPU ComputeClass). Run deploy_app.sh after
# this to build/push the image and deploy the controller.
#
# The Hub IGNORES this file — it assumes a live cluster, installs cluster/ during
# build_infra.sh, and applies infra/ per deploy.
set -e

# --- Load configuration ----------------------------------------------------
if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found. Create one with: cp .env.example .env"
  exit 1
fi

for cmd in gcloud kubectl python3; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: $cmd is required but not installed."
    exit 1
  fi
done

REGION="${REGION:-${ZONE%-*}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Mode dispatch ---------------------------------------------------------
#   (no flag)         create cluster + prerequisites
#   --delete          remove cluster-scoped prereqs (keep the cluster)
#   --delete-cluster  the above, plus delete the GKE cluster
MODE="create"
case "${1:-}" in
  --delete)         MODE="delete" ;;
  --delete-cluster) MODE="delete-cluster" ;;
  -h|--help)        echo "Usage: $0 [--delete | --delete-cluster]"; exit 0 ;;
  "")               MODE="create" ;;
  *) echo "Unknown argument: $1 (use --delete, --delete-cluster, or no flag)"; exit 1 ;;
esac

cluster_exists() {
  gcloud container clusters describe "${CLUSTER_NAME}" \
    --zone="${ZONE}" --project="${PROJECT_ID}" &>/dev/null
}

if [ "$MODE" = "delete" ] || [ "$MODE" = "delete-cluster" ]; then
  if cluster_exists; then
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"
    echo "=== Removing cluster-scoped prerequisites ==="
    # Single kustomization (operator bundle + jobset-system ns + Spot ComputeClass),
    # mirroring the Hub's `apply -k cluster/`. --ignore-not-found for clean partial teardown.
    kubectl delete -k "${ROOT}/cluster" --ignore-not-found || true
  else
    echo "Cluster ${CLUSTER_NAME} does not exist; nothing to remove."
  fi
  if [ "$MODE" = "delete-cluster" ] && cluster_exists; then
    echo "=== Deleting GKE cluster ${CLUSTER_NAME} (several minutes) ==="
    gcloud container clusters delete "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" --quiet || true
  fi
  echo "=== Teardown complete ==="
  exit 0
fi

# --- Step 1: Create the GKE cluster ---------------------------------------
# Node Auto-Provisioning is required so the jobset-cpu ComputeClass can create
# Spot CPU node pools on demand for the JobSet leader/worker pods. The small
# default pool hosts the JobSet operator and the controller.
echo "=== Step 1: Creating GKE cluster ${CLUSTER_NAME} (${ZONE}) ==="
if cluster_exists; then
  echo "Cluster ${CLUSTER_NAME} already exists. Skipping creation."
else
  gcloud container clusters create "${CLUSTER_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --num-nodes="${NUM_NODES}" \
    --gateway-api=standard \
    --enable-autoprovisioning \
    --min-cpu 0 --max-cpu "${MAX_CPU:-200}" \
    --min-memory 0 --max-memory "${MAX_MEMORY:-800}"
fi

echo "=== Step 2: Getting cluster credentials ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}"

# --- Step 3: Cluster-scoped prerequisites (one kustomization) -------------
echo "=== Step 3: Installing cluster prerequisites (JobSet operator + ns + Spot ComputeClass) ==="
# A single top-level kustomization composes the JobSet operator bundle (pinned
# release), the jobset-system Namespace (apply -k won't create it otherwise), and
# the Spot CPU ComputeClass — the exact same dir and command the Hub's
# build_infra.sh runs, so standalone and Hub never drift. Server-side apply because
# JobSet's CRDs exceed the client-side 256KB annotation limit.
kubectl apply --server-side --force-conflicts -k "${ROOT}/cluster"
echo "=== Waiting for the JobSet controller to be Ready ==="
kubectl -n jobset-system rollout status deploy/jobset-controller-manager --timeout=300s || true

echo "=== Setup complete. Next: ./deploy_app.sh ==="
