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
                    ┌──────────────▼──────────────┐
                    │         RabbitMQ             │
                    │  mgn_jobs  poll_jobs         │
                    │  rollback_jobs   dlx.*       │
                    └──────┬──────────┬────────────┘
                           │          │
               ┌───────────▼──┐  ┌────▼───────────┐
               │  MGN Worker  │  │  Poller Worker  │
               │              │  │                 │
               └───────┬──────┘  └────────┬────────┘
                       │                  │
               ┌───────▼──────────────────▼────────┐
               │           State Manager            │
               │     (PostgreSQL — single source    │
               │      of truth for all server       │
               │      states, row-level locking)    │
               └───────────────────────────────────┘
```

**MGN Worker** — executes migration actions by calling AWS MGN and EC2 APIs (agent install, replication setup, instance launch, cutover, cleanup).

**Poller Worker** — monitors long-running AWS operations and advances server state when they complete.

**Rollback Worker** — consumes from dead-letter queues, undoes completed steps in reverse order, and transitions servers to `FAILED` (clean state) or `FROZEN` (unknown state, human intervention required).

**Orchestrator API** — FastAPI service for engineers to register servers, resolve approval gates, monitor migration progress, and manually intervene on failed servers.

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
  FROZEN  — rollback failed, server in unknown state, human must intervene
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
| AWS integration | boto3 | MGN and EC2 API calls |

---

## Repository Structure

```
k8s/
  rabbitmq/     StatefulSet, services, secret, configmap
  postgres/     StatefulSet, services, secret

src/
  dispatcher/   Job dispatch logic and job type definitions
  workers/      Poller worker, MGN worker, Rollback worker
  state_manager/  State machine definition, transition validator, DB operations
  db/           Alembic migrations
```

---

## Status

Active development. Core infrastructure complete (RabbitMQ, PostgreSQL, state machine, dispatcher, poller). MGN Worker, Rollback Worker, and Orchestrator API in progress.
