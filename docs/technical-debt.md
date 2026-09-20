# Technical Debt And Risk Register

This document records current code-level risks in priority order. It is not a feature wishlist.

## P1: Related Recommendation State Can Lag A Saved Viewing Record

The Add watched path now saves `viewing_history` and a replacing `sheet_sync_outbox` task in one PostgreSQL transaction. The frontend keeps a `history_id` in its draft, and Sheet upserts use the same UUID in `RecordId`, so retrying a record does not require another blind append. After that save, `routes.py` separately marks wishlist or recommendation items watched and deactivates candidate rows. A failure there returns a warning while the history record remains saved, with no automatic repair of the related state.

Required direction:

- reconcile saved history with wishlist, recommendation-item, and candidate-pool state after a partial failure
- define transaction boundaries for those related state changes where they share one PostgreSQL database
- surface the returned warning in the Add watched UI so users can see incomplete follow-up work

## P1: Desktop Local API Security Boundary Is Too Broad

The backend accepts the opaque `null` origin for the Electron `file://` frontend, and mutation endpoints have no local authentication token. Electron opens external HTTP(S) links in the system browser, but its window handler allows other URL schemes and the navigation handler only intercepts external HTTP(S) destinations.

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
- make the existing Google Sheets dry-run credential check part of a repeatable integration target
- add failure-path tests for partial Add watched completion

## P2: The Frontend Is A Single Large Module

`frontend/src/main.tsx` still owns the view components and most workflow state. API transport, shared cards, storage helpers, and pagination hooks have been extracted, but the main module remains large and has no focused frontend test target.

Required direction:

- split each main view into a module
- add focused tests for draft persistence, source-tab return, processed-card state, and failure handling

## P2: Browser Dev Proxy Omits Candidate Queue

`frontend/vite.config.ts` proxies the main API paths but not `/candidate-queue`. The queue control calls that path through the shared API client, so it cannot reach FastAPI through the default Vite origin without an explicit `VITE_API_BASE_URL`.

Required direction:

- proxy `/candidate-queue` in browser development, or set a documented API base URL for that mode
- add a small browser development smoke check for the queue status control

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
