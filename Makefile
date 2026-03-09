.PHONY: help test test-docker test-kind build up down kind-teardown

IMAGE_NAME  ?= migration-demo
IMAGE_TAG   ?= latest
KIND_CLUSTER ?= migration-demo

help:
	@echo ""
	@echo "  make test          Run unit/integration tests locally (testcontainers)"
	@echo "  make test-docker   Run migration + app via Docker Compose"
	@echo "  make test-kind     Deploy to a local kind cluster and verify migrations"
	@echo ""
	@echo "  make build         Build the Docker image"
	@echo "  make up            Start docker-compose stack (postgres + migrate + app)"
	@echo "  make down          Tear down docker-compose stack and volumes"
	@echo "  make kind-teardown Delete the kind cluster"
	@echo ""
	@echo "  Variables (override on command line):"
	@echo "    IMAGE_NAME=$(IMAGE_NAME)  IMAGE_TAG=$(IMAGE_TAG)  KIND_CLUSTER=$(KIND_CLUSTER)"
	@echo ""

# ---------------------------------------------------------------------------
# Local pytest (testcontainers spins up Postgres automatically)
# ---------------------------------------------------------------------------
test:
	pytest -v

# ---------------------------------------------------------------------------
# Docker Compose — exercises the full migration flow as a single-node Docker
# setup, with the migrate service standing in for the initContainer.
# ---------------------------------------------------------------------------
test-docker: build
	@echo "Running migration via docker compose..."
	docker compose run --rm migrate
	@echo "Verifying app starts and schema is accessible..."
	docker compose up -d app
	@sleep 3
	docker compose exec app python -c \
		"import urllib.request, json; \
		 r = urllib.request.urlopen('http://localhost:8000/healthz'); \
		 print('healthz:', json.loads(r.read()))"
	docker compose exec app python -c \
		"import urllib.request, json; \
		 r = urllib.request.urlopen('http://localhost:8000/items'); \
		 print('items:', json.loads(r.read()))"
	@echo ""
	@echo "Docker test PASSED"
	docker compose down -v

build:
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

up: build
	docker compose up

down:
	docker compose down -v

# ---------------------------------------------------------------------------
# Kubernetes / kind — exercises real concurrent initContainer migrations with
# 3 replicas. Requires kind and kubectl.
# ---------------------------------------------------------------------------
test-kind:
	@command -v kind >/dev/null 2>&1 || \
		{ echo "ERROR: kind not found. Install from https://kind.sigs.k8s.io/docs/user/quick-start/"; exit 1; }
	KIND_CLUSTER=$(KIND_CLUSTER) ./scripts/test-kind.sh

kind-teardown:
	kind delete cluster --name $(KIND_CLUSTER)
