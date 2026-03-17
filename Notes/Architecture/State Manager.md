#### State manager is a centralised database that stores the current state of all the source servers. This is the main source of truth which will constantly be updated as the source servers are getting migrated. 

In this case, PostgreSQL is used as every other alternative falls short. Redis is very fast, but it is a cache, meaning if the server restarts in between state transitions then all data will be lost. Simple filesystem structure will bring about race conditions and concurrency issues when multiple pods are trying to access the files at the same time. 

## Components:
### 1. Servers Table
This table would store all the information about the source server, its current state, etc.
 
**Fields:**
  - server_id — primary key, unique identifier for the source server
  - hostname — human-readable name of the source server
  - ip_address — source server IP
  - aws_source_server_id — the ID MGN assigns when the agent is installed
  - current_state — the server's current position in the state machine (e.g. REPLICATION_STARTED)
  - previous_state — the state it was in before the last transition, useful for rollback and auditing
  - assigned_engineer — who is responsible for this migration
  - created_at — when the server was added to the system
  - updated_at — when the state last changed

### 2. State Transition Manager Table 
This is an immutable, append-only table that contains information about all the state transitions that have taken place. It is imperative to have this table implemented as this is the main point of reference for the rollback worker to see what the last valid state of the server is. 

**Fields:**
  - transition_id — primary key
  - server_id — foreign key to servers table
  - from_state — the state the server was in before this transition
  - to_state — the state the server moved to
  - job_id — the job that caused this transition, traceable back to the original message
  - job_type — which job type triggered it (e.g. start_replication)
  - triggered_by — either system (automated) or the engineer's ID (manual/approval gate)
  - timestamp — when this transition happened
  - metadata — JSON field for any additional context (e.g. the AWS instance ID that was launched, error message if this was a failure transition)

### 3. State Machine Definition 
This is a module that contains the list of valid states for a server as well as the valid transition between the states. This is what helps ensure linearity. 

**Valid states — in order:**

  Stage 1 — Onboarding
    PENDING
    AGENT_INSTALLED

  Stage 2 — Replication Setup
    REPLICATION_CONFIGURED
    AWAITING_REPLICATION_APPROVAL       ← human gate: security group rules
    REPLICATION_STARTED
    READY_FOR_TESTING

  Stage 3 — Test Launch
    TEST_LAUNCH_TEMPLATE_CONFIGURED
    AWAITING_TEST_LAUNCH_APPROVAL       ← human gate: launch template review
    TEST_INSTANCE_LAUNCHING
    TEST_INSTANCE_RUNNING
    AWAITING_TEST_VALIDATION            ← human gate: validate test instance works
    TEST_FINALIZED

  Stage 4 — Cutover
    CUTOVER_LAUNCH_TEMPLATE_CONFIGURED
    AWAITING_CUTOVER_LAUNCH_APPROVAL    ← human gate: cutover launch template review
    CUTOVER_STARTED
    CUTOVER_SYNC_IN_PROGRESS
    READY_FOR_CUTOVER_LAUNCH
    CUTOVER_INSTANCE_LAUNCHING
    CUTOVER_INSTANCE_RUNNING
    AWAITING_CUTOVER_VALIDATION         ← human gate: validate production instance works
    CUTOVER_FINALIZED

  Stage 5 — Post-Migration Cleanup
    DISCONNECTING
    DISCONNECTED
    AWAITING_ARCHIVE_APPROVAL           ← human gate: confirm safe to archive from MGN
    ARCHIVED
    AWAITING_CLEANUP_APPROVAL           ← human gate: confirm safe to delete replication infra
    CLEANUP_COMPLETE

  Error States
    FAILED    — job failed, rollback succeeded, server in known clean state
    FROZEN    — rollback failed, server in unknown state, human must intervene

**Examples of legal transitions between states:**
  PENDING                            → { AGENT_INSTALLED }
  AGENT_INSTALLED                    → { REPLICATION_CONFIGURED }
  REPLICATION_CONFIGURED             → { AWAITING_REPLICATION_APPROVAL }
  AWAITING_REPLICATION_APPROVAL      → { REPLICATION_STARTED, FAILED }
  REPLICATION_STARTED                → { READY_FOR_TESTING }
  READY_FOR_TESTING                  → { TEST_LAUNCH_TEMPLATE_CONFIGURED }
  TEST_LAUNCH_TEMPLATE_CONFIGURED    → { AWAITING_TEST_LAUNCH_APPROVAL }
  AWAITING_TEST_LAUNCH_APPROVAL      → { TEST_INSTANCE_LAUNCHING, FAILED }
  TEST_INSTANCE_LAUNCHING            → { TEST_INSTANCE_RUNNING }
  TEST_INSTANCE_RUNNING              → { AWAITING_TEST_VALIDATION }
  AWAITING_TEST_VALIDATION           → { TEST_FINALIZED, FAILED }
  ...
  DISCONNECTED                       → { AWAITING_ARCHIVE_APPROVAL }
  AWAITING_ARCHIVE_APPROVAL          → { ARCHIVED, FAILED }
  ARCHIVED                           → { AWAITING_CLEANUP_APPROVAL }
  AWAITING_CLEANUP_APPROVAL          → { CLEANUP_COMPLETE, FAILED }

  All active and waiting states can also transition to FROZEN (rollback failure).

### 4. Transaction Validator 
Before any state transition is implemented in the database there exists a function to check if the state transition that is going to be made by a certain module is valid or not. If yes, it allows the rewriting on the state in the servers table and an update in the state transition history table. If not, it prevents the write. 

### 5. Row-level Locking
The row level locking mechanism has to be implemented to ensure that no two workers executing a job for a source server has the ability to manipulate the state at the same time, which could cause duplicate entries in the state transaction history table as well and unintended rewrites. 

the **SELECT FOR UPDATE** command in PostgreSQL ensures that a row is locked for the duration of the transaction so no other connection can read or write it till the transaction is over. 

### 6. Approval Gate Records Table
Some writes to the servers/ state transition history table are made through human approvals, not automated jobs. This table helps keep a track of the approvals that have been made by engineers (for example, going ahead with test/final cutover, configuring the launch template, etc)

**Fields:**
  - approval_id — primary key
  - server_id — which server is waiting
  - gate_type — what is being approved (e.g. APPROVE_TEST_LAUNCH_TEMPLATE)
  - status — PENDING, APPROVED, REJECTED
  - requested_at — when the gate was triggered
  - resolved_at — when a human acted on it
  - resolved_by — who approved or rejected it
  - notes — optional human comment


![[img11.png]]
