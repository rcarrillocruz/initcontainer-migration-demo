#!/usr/bin/env bash
# test-kind.sh — deploy the migration demo to a local kind cluster and verify
# that all three replicas start cleanly after running concurrent initContainer
# migrations with pg_advisory_lock.
#
# Usage:
#   ./scripts/test-kind.sh              # create cluster, test, delete cluster
#   KEEP_CLUSTER=1 ./scripts/test-kind.sh  # keep cluster after test (re-use next run)
#
# Prerequisites: kind, kubectl, docker

set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER:-migration-demo}"
NAMESPACE="migration-demo"
IMAGE_NAME="migration-demo:latest"
TIMEOUT="${KIND_TIMEOUT:-180s}"
KEEP_CLUSTER="${KEEP_CLUSTER:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
for cmd in kind kubectl docker; do
    command -v "$cmd" &>/dev/null || die "$cmd is required but not found"
done

# ---------------------------------------------------------------------------
# Cluster lifecycle
# ---------------------------------------------------------------------------
cluster_exists() {
    kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"
}

if cluster_exists; then
    log "Reusing existing kind cluster: $CLUSTER_NAME"
else
    log "Creating kind cluster: $CLUSTER_NAME"
    kind create cluster --name "$CLUSTER_NAME"
fi

teardown() {
    if [[ "$KEEP_CLUSTER" == "1" ]]; then
        log "KEEP_CLUSTER=1 — cluster '$CLUSTER_NAME' left running"
    else
        log "Deleting kind cluster: $CLUSTER_NAME"
        kind delete cluster --name "$CLUSTER_NAME"
    fi
}
trap teardown EXIT

# ---------------------------------------------------------------------------
# Build and load image into kind
# ---------------------------------------------------------------------------
log "Building Docker image: $IMAGE_NAME"
docker build -t "$IMAGE_NAME" "$ROOT_DIR" --quiet

log "Loading image into kind cluster"
kind load docker-image "$IMAGE_NAME" --name "$CLUSTER_NAME"

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
log "Applying Kubernetes manifests"
kubectl apply -f "$ROOT_DIR/k8s/namespace.yaml"
kubectl apply -f "$ROOT_DIR/k8s/postgres.yaml"
kubectl apply -f "$ROOT_DIR/k8s/secret.yaml"

log "Waiting for postgres to become ready..."
kubectl rollout status deployment/postgres \
    --namespace="$NAMESPACE" \
    --timeout="$TIMEOUT"

log "Deploying app (3 replicas, each with initContainer migration)"
kubectl apply -f "$ROOT_DIR/k8s/deployment.yaml"
kubectl apply -f "$ROOT_DIR/k8s/service.yaml"

# ---------------------------------------------------------------------------
# Wait for all pods to pass their initContainers and reach Running
# ---------------------------------------------------------------------------
log "Waiting for all pods to become ready (this exercises concurrent pg_advisory_lock)..."
kubectl rollout status deployment/migration-demo \
    --namespace="$NAMESPACE" \
    --timeout="$TIMEOUT"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
log "Pod status:"
kubectl get pods --namespace="$NAMESPACE" -o wide

log "Init container logs (db-migrate) from each pod:"
kubectl get pods --namespace="$NAMESPACE" -l app=migration-demo \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
| while read -r pod; do
    echo ""
    echo "--- $pod ---"
    kubectl logs "$pod" \
        --namespace="$NAMESPACE" \
        --container=db-migrate \
        2>/dev/null || echo "(no logs — container may have exited cleanly)"
done

# ---------------------------------------------------------------------------
# Verify schema via psql inside the postgres pod
# ---------------------------------------------------------------------------
log "Verifying final schema inside the postgres pod..."
PG_POD=$(kubectl get pods --namespace="$NAMESPACE" \
    -l app=postgres \
    -o jsonpath='{.items[0].metadata.name}')

log "Tables present:"
kubectl exec "$PG_POD" --namespace="$NAMESPACE" -- \
    psql -U appuser -d appdb -c \
    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;"

log "Alembic version:"
kubectl exec "$PG_POD" --namespace="$NAMESPACE" -- \
    psql -U appuser -d appdb -c "SELECT version_num FROM alembic_version;"

# Assert version is at head
VERSION=$(kubectl exec "$PG_POD" --namespace="$NAMESPACE" -- \
    psql -U appuser -d appdb -t -c "SELECT version_num FROM alembic_version;" \
    | tr -d '[:space:]')

if [[ "$VERSION" == "002" ]]; then
    log "Schema is at expected revision 002 — all migrations applied correctly."
else
    die "Unexpected alembic_version: '$VERSION' (expected '002')"
fi

log "Kind test PASSED — all 3 replicas started with concurrent initContainer migrations."
