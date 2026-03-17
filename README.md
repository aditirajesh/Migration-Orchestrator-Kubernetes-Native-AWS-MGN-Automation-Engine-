# Migration Orchestrator: Kubernetes-Native AWS MGN Automation Engine

A distributed system that automates end-to-end server migrations using AWS Application Migration Service (MGN). Built on Kubernetes with RabbitMQ for job coordination and PostgreSQL for state persistence, it enforces a strict 27-state machine across the full migration lifecycle — from agent installation through final cleanup — with human approval gates at every critical decision point.

---

## The Problem

Migrating servers to AWS with MGN involves a long sequence of steps across multiple stages: agent installation, replication configuration, test launches, cutover, and post-migration cleanup. Each step requires AWS API calls, status polling, and human validation at key points. Done manually, this process is error-prone, difficult to audit, and impossible to parallelise safely across a large server fleet.

---

## What This System Does

- Coordinates the full migration lifecycle for multiple servers in parallel
- Enforces state machine rules so no server can skip steps or move backwards
- Pauses automatically at human approval gates (replication review, launch template review, test validation, cutover validation, archive and cleanup confirmation)
- Polls long-running AWS operations (replication sync, instance launch) asynchronously without blocking
- Runs pre-flight replication health checks before test and cutover launches to prevent partial data copies
- Rolls back automatically when a job fails, landing the server in a known clean state
- Maintains a complete, immutable audit trail of every state transition — who triggered it, which job caused it, and when it happened

---

## Architecture

```
                        ┌─────────────────────┐
                        │   Orchestrator API   │  ← engineers interact here
                        │      (FastAPI)        │
                        └──────────┬──────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │            RabbitMQ              │
                    │  mgn_jobs        poll_jobs       │
                    │      │               │           │
                    │  [dlx.migration exchange]        │
                    │      │               │           │
                    │  mgn_jobs.failed  poll_jobs.failed│
                    └──────┬───────────────┬───────────┘
                           │               │
               ┌───────────▼──┐  ┌─────────▼──────┐
               │  MGN Worker  │  │  Poller Worker  │
               └───────┬──────┘  └────────┬────────┘
                       │                  │         \
                       │                  │    ┌─────▼──────────┐
                       │                  │    │ Rollback Worker │
                       │                  │    │ (mgn+poll.failed│
                       │                  │    └─────────────────┘
               ┌───────▼──────────────────▼────────┐
               │           State Manager            │
               │     (PostgreSQL — single source    │
               │      of truth for all server       │
               │      states, row-level locking)    │
               └───────────────────────────────────┘
```

**MGN Worker** — executes migration actions by calling AWS MGN APIs across all 13 job types (replication setup, test launch, cutover, cleanup). Includes pre-flight replication health checks before test and cutover launches.

**Poller Worker** — monitors long-running AWS operations via MGN job and lifecycle state polling. Re-enqueues itself until operations complete, then advances server state.

**Rollback Worker** — consumes from `mgn_jobs.failed` and `poll_jobs.failed`. Executes the minimum AWS undo actions for the server's current state, then marks it `FAILED` (clean, recoverable) or `FROZEN` (unknown state, human intervention required).

**Orchestrator API** — FastAPI service for engineers to register servers, resolve approval gates, monitor migration progress, query audit history, and manage batch (wave) migrations. Exposes 51 endpoints across per-server pipeline actions, bulk operations, batch management, and global history.

---

## State Machine

Servers progress through 27 states across 5 stages. The system enforces every transition — no worker can skip a step, re-enter a completed state, or move a server that is waiting at a human gate.

```
Stage 1  Onboarding          PENDING → AGENT_INSTALLED
Stage 2  Replication         REPLICATION_CONFIGURED → ... → READY_FOR_TESTING
Stage 3  Test Launch         TEST_LAUNCH_TEMPLATE_CONFIGURED → ... → TEST_FINALIZED
Stage 4  Cutover             CUTOVER_LAUNCH_TEMPLATE_CONFIGURED → ... → CUTOVER_FINALIZED
Stage 5  Cleanup             DISCONNECTING → ... → CLEANUP_COMPLETE

Human gates (AWAITING_*):   replication approval, test launch approval,
                             test validation, cutover launch approval,
                             cutover validation, archive approval, cleanup approval

Error states:
  FAILED  — job failed, rollback succeeded, server in known clean state
  FROZEN  — rollback failed or cutover committed, human must intervene
```

---

## Tech Stack

| Component | Technology | Reason |
|---|---|---|
| Job queue | RabbitMQ (StatefulSet) | Durable messaging, dead-letter routing, at-least-once delivery |
| State store | PostgreSQL (StatefulSet) | ACID transactions, row-level locking, queryable audit trail |
| Workers | Python + asyncio + aio-pika | Async I/O for concurrent job processing without thread overhead |
| DB access | asyncpg + Alembic | Async PostgreSQL driver, version-controlled schema migrations |
| Orchestration | Kubernetes (kind for local) | Portable, production-grade deployment |
| AWS integration | boto3 | MGN API calls across all migration stages |

---

## Dockerfiles

Two images cover the entire system:

**`docker/Dockerfile.workers`** — shared image for all three workers and the one-time Alembic migration job. All workers are built from the same codebase and share the same dependencies, so a single image is sufficient. Each Kubernetes deployment overrides the `command` field to run the correct entry point (`run_mgn`, `run_poller`, `run_rollback`). The Alembic migration Job overrides it further to run `alembic upgrade head`.

**`docker/Dockerfile.api`** — separate image for the Orchestrator API. Kept separate so workers and API can be built and versioned independently; a change to the API does not force a workers rebuild.

Both images set `WORKDIR /app/src`. All worker entry points call `sys.path.insert(0, ".")` at startup, which adds the working directory to Python's module search path — placing `WORKDIR` at `src/` makes every package (`workers`, `state_manager`, `dispatcher`, `api`) importable without any additional path manipulation.

---

## Kubernetes Deployments

All components run inside a `migration-orchestrator` namespace. Credentials are never baked into images — they are injected at runtime from Kubernetes Secrets.

| Manifest | Kind | Notes |
|---|---|---|
| `k8s/db-migrate/job.yaml` | Job | Runs `alembic upgrade head` once before workers start. Uses `postgresql://` (synchronous) because Alembic does not use asyncpg. `backoffLimit: 3`, `restartPolicy: Never`. |
| `k8s/workers/mgn-worker/deployment.yaml` | Deployment | 2 replicas. Injects AWS credentials for MGN + EC2 API calls. |
| `k8s/workers/poller-worker/deployment.yaml` | Deployment | 2 replicas. Injects AWS credentials — the poller calls EC2 `describe_instance_status` for 2/2 reachability checks before advancing to test/cutover validation gates. |
| `k8s/workers/rollback-worker/deployment.yaml` | Deployment | 1 replica (rollback is serialised per server). Injects AWS credentials for undo actions. |
| `k8s/api/deployment.yaml` | Deployment | 1 replica. No AWS credentials needed — only talks to RabbitMQ and PostgreSQL. |
| `k8s/api/service.yaml` | Service (NodePort) | Exposes the API on NodePort 30800. |
| `k8s/kind-cluster.yaml` | kind config | Maps NodePort 30800 → `localhost:8000` via `extraPortMappings`, so the API is reachable without `kubectl port-forward`. |

Connection URLs (`DATABASE_URL`, `AMQP_URL`) are composed at pod startup using Kubernetes env var substitution — individual secret keys are injected as named env vars, then referenced with `$(VAR_NAME)` to build the full connection string.

---

## Repository Structure

```
docker/
  Dockerfile.workers  Shared image: mgn-worker, poller-worker, rollback-worker, db-migrate job
  Dockerfile.api      Orchestrator API image

k8s/
  kind-cluster.yaml   Cluster definition + extraPortMappings (NodePort 30800 → localhost:8000)
  namespace.yaml      migration-orchestrator namespace
  rabbitmq/           StatefulSet, services, secret, configmap
  postgres/           StatefulSet, services, secret
  aws-credentials/    Kubernetes Secret for AWS STS credentials (placeholder)
  iam/                IAM policy document for the worker role
  db-migrate/         One-time Alembic migration Job
  workers/
    mgn-worker/       Deployment (2 replicas)
    poller-worker/    Deployment (2 replicas)
    rollback-worker/  Deployment (1 replica)
  api/                Deployment + NodePort Service

src/
  dispatcher/        Job type definitions and queue routing
  state_manager/     State machine (27 states, transition validator, DB operations)
  workers/
    mgn_worker.py       All 13 MGN job handlers + pre-flight checks
    poller_worker.py    5 poll handlers (replication, test, cutover, disconnect)
    rollback_worker.py  Undo logic mapped to every state, FAILED/FROZEN finalisation
    aws_clients.py      boto3 client factory
    state_manager_client.py  Worker-facing DB interface
    run_mgn.py / run_poller.py / run_rollback.py  Entry points
  api/
    main.py             FastAPI app + lifespan (DB pool, RabbitMQ connection)
    models.py           Pydantic request/response models
    dependencies.py     FastAPI dependency injection (StateManager, JobDispatcher)
    routes/
      servers.py        25 per-server endpoints + 2 bulk endpoints
      batches.py        6 batch management endpoints + 17 batch pipeline action endpoints
      history.py        GET /history — global audit trail with composable filters
    run_api.py          uvicorn entry point
  db/                Alembic migrations (servers, state_transition_history, batches)
```

---

## What Each Worker Publishes

| Worker | Publishes to | When |
|---|---|---|
| MGN Worker | `poll_jobs` | After start_replication, start_test, start_cutover, disconnect |
| MGN Worker | `mgn_jobs` | After finalize_cutover (dispatches DISCONNECT_SOURCE_SERVER) |
| Poller Worker | `poll_jobs` | Re-enqueues itself when an operation is still in progress |
| Rollback Worker | Nothing | Reads only — writes final state to PostgreSQL |

---

## RabbitMQ Dead-Letter Setup

All three main queues (`mgn_jobs`, `poll_jobs`, `rollback_jobs`) are configured with `x-dead-letter-exchange=dlx.migration`. The `dlx.migration` exchange routes failed messages to isolated `.failed` queues (`mgn_jobs.failed`, `poll_jobs.failed`, `rollback_jobs.failed`). The Rollback Worker consumes from `mgn_jobs.failed` and `poll_jobs.failed` directly, preserving per-worker failure visibility.

---

## Orchestrator API

The API is the only external interface to the system. Engineers interact exclusively through it — no direct database access or queue publishing outside of the API and workers.

**51 endpoints across four categories:**

| Category | Count | Description |
|---|---|---|
| Per-server pipeline actions | 25 | Register, start, configure, approve/reject gates, reset |
| Bulk operations | 2 | Bulk register (`POST /servers/bulk`), bulk start (`POST /servers/bulk-start`) — HTTP 207 Multi-Status, per-item results |
| Batch management | 6 | Create batch, list, get, add servers, get servers, get audit history |
| Batch pipeline actions | 17 | Same gate actions as per-server but applied to an entire wave simultaneously |
| Global history | 1 | `GET /history` — composable filters across server, batch, state, engineer, job type, and time range |

**Batch (wave) support:**
Servers can be grouped into named batches and progressed through the pipeline together. A batch action iterates all servers in the wave and returns a three-way result — `succeeded` / `skipped` / `failed` — so one server falling behind does not block the rest. The three configure endpoints (`configure-replication`, `configure-test-launch`, `configure-cutover-launch`) store a JSONB config snapshot on the batch record as an audit reference.

**Query filters:**
`GET /servers` accepts four composable filters: `state`, `batch_id`, `assigned_engineer`, `hostname` (partial match). Use `batch_id=unassigned` to list servers not yet in any batch.

**Schema additions for batch support:**
Two Alembic migrations extend the original schema — `a3f9c12e8b47` adds `aws_account_id` and `aws_region` to `servers`; `b5e8d4a1c9f2` creates the `batches` table and adds a nullable `batch_id` FK to `servers` with `ON DELETE SET NULL`.

---

## Status

| Component | Status |
|---|---|
| PostgreSQL StatefulSet | Complete |
| RabbitMQ StatefulSet + DLX | Complete |
| State machine (27 states, transitions) | Complete |
| Alembic migrations | Complete |
| Job Dispatcher | Complete |
| MGN Worker (all 13 handlers) | Complete |
| Poller Worker (all 5 handlers) | Complete |
| Rollback Worker | Complete |
| Orchestrator API (FastAPI) | Complete |
| Dockerfiles + k8s Deployments | Complete |
| End-to-end test | Pending |
