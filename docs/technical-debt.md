# Technical Debt And Risk Register

This document records current code-level risks in priority order. It is not a feature wishlist.

## P0: Add Watched Is Not Idempotent Across Google Sheets And PostgreSQL

The interactive record path appends Google Sheets first, then writes PostgreSQL and recommendation/wishlist state. If Sheets succeeds and a later local write fails, the API returns an error even though the source-of-truth row already exists. A user retry can append a duplicate Sheets row.

Required direction:

- introduce an explicit client-generated operation/idempotency key that survives retries
- return stage-aware failure information when Sheets succeeded but local persistence failed
- add a recovery/reconciliation path that completes local persistence without appending another row
- prevent application shutdown from interrupting an in-flight record operation without warning

## P1: Desktop Local API Security Boundary Is Too Broad

The backend accepts the opaque `null` origin for the Electron `file://` frontend, and mutation endpoints have no local authentication token. The Electron window also lacks explicit navigation and new-window restrictions.

Required direction:

- replace `file://` plus `allow_origins=["null"]` with a constrained custom protocol or local authenticated origin
- generate a per-launch API token and pass it through the preload bridge
- enable Electron sandboxing where compatible
- deny unexpected navigation and handle approved external links explicitly

## P1: Long-Lived Shared Database Connections Need A Concurrency Model

The recommendation and viewing-history services keep long-lived repository instances and psycopg connections in process-global objects. FastAPI sync routes can run concurrently in worker threads. Some recommendation operations use a lock, but the full repository surface does not have one consistent concurrency contract.

Required direction:

- use a PostgreSQL connection pool with request/transaction-scoped connections
- define transaction boundaries for feedback, wishlist, recommendation-item processing, and candidate-pool state changes
- remove process-global mutable repository caches or give them explicit invalidation and locking rules

## P1: Integration Coverage Does Not Match The Real Runtime

Most tests exercise in-memory repositories or mocked external services. PostgreSQL integration tests are optional/skipped, and desktop smoke checks are manual commands rather than a repeatable test target.

Required direction:

- make PostgreSQL integration tests runnable in one documented command
- add an Electron smoke test target that proves first paint, backend readiness, poster loading, and process cleanup
- add a non-writing Google Sheets authentication check
- add failure-path tests for partial Add watched completion

## P2: The Frontend Is A Single Large Module

`frontend/src/main.tsx` owns API transport, storage, recommendation state, wishlist state, Add watched state, and all view components. This makes behavior changes risky and leaves no focused frontend test seams.

Required direction:

- extract a typed API client
- extract storage/state hooks by workflow
- split each main view into a module
- add focused tests for draft persistence, source-tab return, processed-card state, and failure handling

## P2: Desktop Delivery Is Still A Developer Launcher

The current desktop experience depends on the repository, Node dependencies, a built frontend, `.venv`, and local configuration files. It is a desktop shell, not yet a distributable desktop product.

Required direction:

- package Electron and the frontend as an installer
- package or provision the Python backend deterministically
- define a user-data/config directory and migration policy
- add versioning, upgrade, and rollback behavior

## P2: Canonical Metadata Encoding Quality Is Visible To Users

Current smoke output and stored metadata include mojibake/person-name corruption. UI formatting cannot reliably repair already-corrupted canonical data.

Required direction:

- audit canonical `movies` text fields for encoding damage
- repair bad rows from raw metadata or controlled refetch
- validate decoded text before canonical upsert

## P3: Operational Details

- `desktop/runtime.log` has no rotation policy.
- The desktop backend health check uses `/openapi.json` instead of a dedicated health endpoint.
- Add watched status messages do not expose the current remote operation stage.
- `today` in the frontend is derived from UTC and can select the wrong local date around midnight.
