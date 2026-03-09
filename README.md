# initContainer Migration Demo with `pg_advisory_lock`

A working reference implementation of the **initContainer pattern** for Kubernetes database
schema migrations, using [Alembic](https://alembic.sqlalchemy.org/) and
[`pg_advisory_lock`](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)
to safely serialise concurrent migrations across multiple replicas.

## Why this exists

The common objection to running DB migrations inside initContainers rather than Kubernetes Jobs
is that multiple replicas start at the same time, so multiple migration runners run concurrently.
This repository demonstrates that the concern is fully addressed at the application level with a
PostgreSQL session-level advisory lock — no operator-level Job lifecycle management required.

## How it works

Each pod's `initContainer` runs `app/migrate.py` before the main app container starts:

```
Pod 1 initContainer        Pod 2 initContainer        Pod 3 initContainer
       |                          |                          |
  pg_advisory_lock() ←────── pg_advisory_lock() ←────── pg_advisory_lock()
       |                    (blocks, waiting)          (blocks, waiting)
  alembic upgrade head
  pg_advisory_unlock()
                                  |
                            lock acquired
                            alembic upgrade head
                            (no-op: already at head)
                            pg_advisory_unlock()
                                                               |
                                                         lock acquired
                                                         alembic upgrade head
                                                         (no-op: already at head)
                                                         pg_advisory_unlock()
       ↓                          ↓                          ↓
  main container            main container            main container
  starts normally           starts normally           starts normally
```

Key properties:

- `pg_advisory_lock(id)` **blocks** until the lock is free — no polling, no busy-wait.
- The lock is **always released in a `finally` block**, even when migration fails.
- On failure, the initContainer exits non-zero → pod stays in `Init:Error` →
  the main app container **never starts** with a broken schema.
- All replicas use the same image — no version skew between migration runner and application code.
- No separate Job object to create, track, re-trigger on upgrade, or clean up via TTL.

## Project structure

```
├── app/
│   ├── migrate.py          # pg_advisory_lock migration runner (the core pattern)
│   ├── main.py             # FastAPI app (items CRUD + /healthz)
│   ├── models.py           # SQLAlchemy models
│   └── database.py         # DB engine + session
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 001_initial_schema.py   # creates items table
│       └── 002_add_job_runs.py     # creates job_runs table
├── k8s/
│   ├── deployment.yaml     # replicas: 3 with initContainer, extensively commented
│   ├── postgres.yaml       # postgres for kind cluster testing
│   ├── secret.yaml         # DATABASE_URL secret
│   ├── namespace.yaml
│   └── service.yaml
├── tests/
│   ├── conftest.py                     # testcontainers Postgres fixture
│   ├── test_migration_lock.py          # lock acquired/released, failure safety
│   └── test_concurrent_migrations.py  # concurrent replica scenarios
├── scripts/
│   └── test-kind.sh        # deploy to kind, verify concurrent pod startup
├── Makefile
├── docker-compose.yml
└── pyproject.toml
```

## Prerequisites

| Tool | Required for | Install |
|------|-------------|---------|
| Python 3.11+ | all targets | system/pyenv |
| Docker | `test-docker`, `test-kind` | [docs.docker.com](https://docs.docker.com) |
| kind | `test-kind` | [kind.sigs.k8s.io](https://kind.sigs.k8s.io/docs/user/quick-start/) |
| kubectl | `test-kind` | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |

## Running the tests

### Option 1 — Local pytest (fastest)

Testcontainers spins up a real Postgres 15 container automatically (Docker daemon must be
running). Python threads simulate concurrent replica startup, with real psycopg2 connections
contending on real advisory locks.

```bash
pip install -e ".[test]"
make test
# or: pytest -v
```

Expected output (abridged):

```
tests/test_concurrent_migrations.py::test_two_concurrent_replicas PASSED
tests/test_concurrent_migrations.py::test_three_concurrent_replicas PASSED
tests/test_concurrent_migrations.py::test_schema_correct_after_concurrent_migrations PASSED
tests/test_concurrent_migrations.py::test_no_deadlock_after_failed_first_runner PASSED
tests/test_concurrent_migrations.py::test_concurrent_with_one_failing PASSED
tests/test_migration_lock.py::test_lock_acquired_and_released PASSED
tests/test_migration_lock.py::test_lock_released_on_failure PASSED
tests/test_migration_lock.py::test_idempotent_migration PASSED
tests/test_migration_lock.py::test_missing_database_url PASSED
9 passed in 62s
```

### Option 2 — Docker Compose

Exercises the full container build, the migration service standing in for the initContainer,
and the FastAPI app starting after migrations complete.

```bash
make test-docker
```

What it does:
1. Builds the Docker image
2. Runs `docker compose run --rm migrate` (migration service waits for postgres, then runs Alembic)
3. Starts the app service
4. Hits `/healthz` and `/items` via `urllib.request` inside the container (slim image has no curl)
5. Tears everything down with `docker compose down -v`

Example migration log:

```
2026-03-09 11:40:37 INFO migrate Connecting for advisory lock (lock_id=7243911227)...
2026-03-09 11:40:37 INFO migrate Waiting for pg_advisory_lock(7243911227)...
2026-03-09 11:40:37 INFO migrate Lock acquired.
2026-03-09 11:40:37 INFO migrate Current revision: None (fresh db) — running 'alembic upgrade head'...
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema - items table
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, Add job_runs table
INFO  [migrate] Migrations applied: None -> 002.
INFO  [migrate] Advisory lock released.
```

The last two lines use a different format because Alembic's `fileConfig()` replaces the root
log handler mid-run. This is cosmetic — all lines are present and correct.

### Option 3 — Kubernetes with kind

Deploys the actual `k8s/deployment.yaml` (3 replicas) to a local kind cluster. All three
pods start their `db-migrate` initContainers concurrently, serialising through
`pg_advisory_lock` against a real in-cluster Postgres. The deployment only rolls out once all
three pods reach `Running`, which proves the initContainers all exited 0.

```bash
make test-kind
```

To keep the cluster running after the test (useful for inspection):

```bash
KEEP_CLUSTER=1 make test-kind
kubectl get pods -n migration-demo
kubectl logs <pod-name> -n migration-demo -c db-migrate
make kind-teardown   # when done
```

Example pod output after a successful run:

```
NAME                              READY   STATUS    RESTARTS   AGE
migration-demo-7c697cd7fd-m4q2g   1/1     Running   0          10s
migration-demo-7c697cd7fd-qqpxq   1/1     Running   0          10s
migration-demo-7c697cd7fd-wjngw   1/1     Running   0          10s
postgres-5f56c6968d-mj5zm         1/1     Running   0          34s
```

Init container logs show the serialisation clearly. The pod that acquires the lock first logs
`Current revision: None (fresh db)` and runs DDL. The others wait, then acquire the lock and
log `Current revision: 002` — Alembic sees schema already at head and no-ops:

```
# Pod 5zd8g — acquired lock first (timestamp :479), ran migrations
2026-03-09 11:02:15 INFO migrate Waiting for pg_advisory_lock(7243911227)...
2026-03-09 11:02:15 INFO migrate Lock acquired.
2026-03-09 11:02:15 INFO migrate Current revision: None (fresh db) — running 'alembic upgrade head'...
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema - items table
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, Add job_runs table
INFO  [migrate] Migrations applied: None -> 002.
INFO  [migrate] Advisory lock released.

# Pod dl5kd — waited ~270ms, acquired lock second (timestamp :696), no-op
2026-03-09 11:02:15 INFO migrate Waiting for pg_advisory_lock(7243911227)...
2026-03-09 11:02:15 INFO migrate Lock acquired.
2026-03-09 11:02:15 INFO migrate Current revision: 002 — running 'alembic upgrade head'...
INFO  [migrate] Schema already at head (002) — no migrations applied (no-op).
INFO  [migrate] Advisory lock released.

# Pod lb2hb — waited ~400ms, acquired lock third (timestamp :051), no-op
2026-03-09 11:02:15 INFO migrate Waiting for pg_advisory_lock(7243911227)...
2026-03-09 11:02:15 INFO migrate Lock acquired.
2026-03-09 11:02:15 INFO migrate Current revision: 002 — running 'alembic upgrade head'...
INFO  [migrate] Schema already at head (002) — no migrations applied (no-op).
INFO  [migrate] Advisory lock released.
```

Lines before `command.upgrade()` use our timestamp format; lines after use alembic.ini's
`[formatter_generic]` format (same cosmetic detail as in Option 2).

Schema verification from inside the postgres pod:

```
table_name
-----------------
 alembic_version
 items
 job_runs

 version_num
 -------------
  002
```

## Makefile targets

```
make test          Run pytest locally (testcontainers)
make test-docker   Build image, run migrate + app via Docker Compose
make test-kind     Deploy to kind cluster, verify all 3 pods start cleanly
make build         Build the Docker image only
make up            Start docker-compose stack interactively
make down          Tear down docker-compose stack and volumes
make kind-teardown Delete the kind cluster
```

Variables you can override:

```bash
IMAGE_NAME=my-app IMAGE_TAG=v1.2.3 KIND_CLUSTER=my-cluster make test-kind
```

## Notes on the lock ID

`MIGRATION_ADVISORY_LOCK_ID = 7243911227` in `app/migrate.py` is an arbitrary stable `int64`.
Pick any value that is unique within your Postgres instance. Since PostgreSQL stores bigint
advisory locks split across two 32-bit columns in `pg_locks` (`classid` = upper bits,
`objid` = lower bits), the lock ID must fit in 64 bits — the tests verify this correctly by
querying both columns rather than just `objid`.
