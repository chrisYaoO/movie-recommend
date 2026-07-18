# macOS Local Build And Data Restore Checklist

Purpose: make the copied Windows project run locally on this Apple Silicon Mac with the data from `movies-postgres-export-20260712-183619.sql`.

This checklist builds a local development-style Electron application. It does **not** package a distributable `.app` or installer; the repository does not currently contain Electron packaging configuration.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` verified during planning, `[?]` requires a decision or explicit approval.

## Safety Rules

- [x] Read `README.md`, `CONTEXT.md`, `docs/mac-codex-build-notes.md`, `docs/desktop.md`, `docs/architecture.md`, `docs/requirements.md`, the database rebuild record, and the relevant runtime/configuration code before building.
- [x] Do not run any `git` command without the user's explicit permission in the current conversation.
- [x] Perform all build work on the `mac-local-build` branch, not `main`.
- [x] Do not start building, installing, restoring, importing, or launching while preparing this checklist.
- [x] Before every state-changing phase, report the exact commands and affected paths/database, then wait for approval.
- [x] Do not directly delete files during this build. Move obsolete or replaceable files into the ignored repository-root `.trash/` directory, preserving relative paths where practical, and report every move.
- [x] Do not move source-of-truth data or secrets into `.trash/` without explicit user approval.
- [x] Never run the Google Sheets rebuild jobs for this migration. The SQL export is the selected source for reproducing the Windows database.
- [x] Never run destructive PostgreSQL tests against the restored `movies` database.
- [x] Keep secrets redacted in terminal output and reports.

## Verified Starting State

- [x] macOS `15.7.4`, Apple Silicon (`arm64`).
- [x] Homebrew is installed.
- [x] Homebrew Python `3.12.12` is installed at `/opt/homebrew/opt/python@3.12/bin/python3.12`.
- [x] Node `26.3.1` and npm `11.16.0` are installed.
- [x] PostgreSQL tools (`psql`, `createdb`) are not installed or not available on `PATH`.
- [x] Google Chrome is not installed in `/Applications`.
- [x] `.env` exists and selects the PostgreSQL recommendation backend and local `movies` database; credentials were not printed.
- [x] `.secrets/google-sheets-service-account.json` exists.
- [x] `data/cache/` exists with 473 files.
- [x] `data/imports/MOVIES.xlsx` exists.
- [x] `.scratch/bandit/latest-model.json` exists.
- [x] `movies-postgres-export-20260712-183619.sql` exists; SHA-256 is `afcfe93a16bdda7625a36dc1e0fb3e9be7b9c51901a847bdebe604de455f1d51`.
- [x] The copied `.venv` is a Windows Python 3.11 environment and has no macOS executable.
- [x] Copied `frontend/node_modules`, `desktop/node_modules`, and `frontend/dist` exist but must not be trusted as Mac builds.
- [x] The selected SQL export is a plain UTF-8 transaction. It drops and recreates nine application tables inside the target database; it does not create roles or databases.

## Expected Restored Snapshot

These counts come from comments generated with the selected SQL export and will be checked after restore:

| Table | Expected rows |
| --- | ---: |
| `candidate_pool` | 2,279 |
| `candidate_subject_queue` | 2,280 |
| `feedback` | 24 |
| `history_recommendation_discovery` | 0 |
| `movies` | 2,415 |
| `recommendation_items` | 520 |
| `recommendation_sessions` | 70 |
| `viewing_history` | 487 |
| `wishlist` | 14 |

## Phase 1: Protect The Copied Source

- [x] Recheck the selected SQL hash before using it.
- [x] Confirm all required local-only assets above remain present and readable.
- [x] Record existing runtime-directory sizes before replacement.
- [x] Obtain approval to move the copied Windows `.venv` and both copied `node_modules` directories into `.trash/`. These are generated runtime files, but moving them changes the working runtime.
- [x] Do not delete the SQL exports, `.env`, `.secrets`, `data/cache`, `data/imports`, or `.scratch/bandit/latest-model.json`.

## Phase 2: Install Only Missing Mac Prerequisites

- [x] Obtain approval to install `postgresql@16` and Google Chrome with Homebrew.
- [x] Install PostgreSQL 16 and Google Chrome; do not reinstall the existing Python 3.12 or Node.
- [x] Put PostgreSQL 16 client tools on the command path for the build session.
- [x] Start the Homebrew PostgreSQL 16 service.
- [x] Verify versions and confirm that the server accepts a local connection before touching the application database.

## Phase 3: Make The Runtime Platform-Aware

- [x] Add the smallest platform-aware change in `desktop/main.cjs`: use `.venv/Scripts/python.exe` on Windows and `.venv/bin/python` on macOS.
- [x] Make the Selenium Chrome default platform-aware in `backend/app/services/metadata_service.py`: retain the Windows path on Windows and use the installed macOS Chrome executable on macOS.
- [x] Add or update focused tests for both path-selection behaviors.
- [x] Add a minimal macOS launcher only if it materially improves repeatable startup; otherwise use `source .venv/bin/activate` followed by `npm --prefix desktop start`, as already documented. No launcher was needed.
- [x] Do not add Electron packagers, process managers, Docker, database migration frameworks, or other new dependencies for this local build.

## Phase 4: Recreate Python And Node Dependencies

- [x] Replace the Windows `.venv` with a new environment created explicitly by Homebrew Python 3.12.
- [x] Upgrade pip inside that virtual environment.
- [x] Install `requirements.txt` and `requirements-dev.txt` into the new environment.
- [x] Replace copied frontend dependencies with a clean `npm ci` from `frontend/package-lock.json`.
- [x] Replace copied desktop dependencies with a clean `npm ci` from `desktop/package-lock.json`.
- [x] Build `frontend/dist` on the Mac with `npm --prefix frontend run build`.
- [x] Confirm the Electron binary and other native Node artifacts are macOS/arm64 artifacts, not copied Windows binaries.

## Phase 5: Create And Restore PostgreSQL Safely

- [x] Parse the database name, host, port, and role from `.env` without displaying the password.
- [x] Check whether the configured role and `movies` database already exist.
- [x] If `movies` already exists or contains tables, stop and ask whether to back it up and replace it; do not run the export over an unknown existing database. The database did not exist.
- [x] Create or adjust the local PostgreSQL role so it matches the DSN, or update `.env` to a dedicated Mac-local role. Do not weaken authentication globally.
- [x] Create an empty `movies` database owned by the selected local role.
- [x] Restore only `movies-postgres-export-20260712-183619.sql` with `ON_ERROR_STOP` enabled so any SQL error aborts the restore. The source file was unchanged; its constraint DDL was reordered in memory so primary keys preceded foreign keys and foreign keys were validated after inserts.
- [x] Confirm the transaction committed successfully and check all nine expected table counts.
- [x] Check foreign-key integrity, the unique viewing-history source-row index, and representative UTF-8 movie text.
- [x] Confirm `.env` still contains `MOVIES_RECOMMENDATION_BACKEND=postgres` and points to this restored database.
- [x] Do not use `jobs.sync_google_sheets_history`, `jobs.rebuild_database`, or `jobs.rebuild_movies_from_history` during snapshot restoration.

## Phase 6: Run Automated Verification

- [x] Run the backend unit suite from the new Mac virtual environment.
- [x] Keep destructive PostgreSQL integration tests skipped for the restored `movies` database. If those tests are needed, create a separate disposable database whose name ends in `_test`.
- [x] Run `npm --prefix frontend run build` and confirm TypeScript/Vite finish successfully.
- [x] Run `npm --prefix desktop test`.
- [x] Start FastAPI on `127.0.0.1:8000` with Selenium prewarm disabled for the first backend smoke test.
- [x] Verify `/openapi.json` and read-only list/search endpoints without creating recommendations, feedback, wishlist changes, or watched records.
- [x] Stop FastAPI and confirm the process exits cleanly.

## Phase 7: Verify The Electron App

- [x] Start the Electron shell from the repository with the new Mac dependencies.
- [x] Confirm it loads the Mac-built `frontend/dist/index.html` and starts FastAPI using `.venv/bin/python`.
- [x] Confirm recommendations, wishlist, not-interested state, posters, and existing history-backed search data render from the restored PostgreSQL snapshot.
- [x] Close the window and verify Electron, uvicorn, chromedriver, and headless Chrome do not remain running.
- [x] Inspect `desktop/runtime.log` for startup or shutdown errors without exposing secrets.

## Phase 8: External-Service Checks And Controlled Write Test

- [x] Ask before any check that contacts Google Sheets or Douban, even when it is intended to be non-writing. The user approved all remaining phases.
- [x] After approval, run the documented Google Sheets credentials dry-run and confirm it does not write PostgreSQL or fetch Douban details.
- [x] Verify Selenium can start macOS Chrome and fetch a Douban detail only after approval for external access.
- [x] Ask before clicking Recommend because that creates recommendation session/item rows in PostgreSQL. The approved Electron smoke created one session and eight items.
- [x] Ask before an end-to-end Add watched test because it appends to Google Sheets first and then changes PostgreSQL. The test was deliberately deferred because no real test movie was selected; no Google Sheets write was made.
- [x] Prefer a disposable database or an explicitly chosen real test movie for state-changing end-to-end verification. No write test was attempted without a selected movie.

## Completion Criteria

- [x] All required dependencies are native to macOS/arm64.
- [x] The restored table counts match the SQL snapshot before any approved state-changing smoke test.
- [x] Backend tests, frontend build, and desktop tests pass.
- [x] The Electron window launches, reads the restored data, loads posters, and shuts down cleanly.
- [x] Google Sheets and Douban checks either pass or are explicitly deferred by the user.
- [x] No source-of-truth rebuild, database overwrite, external write, or cleanup deletion occurred without explicit approval.
- [x] Final report lists code changes, installed prerequisites, verification results, any skipped checks, and the exact remaining limitations.

## Completed Build Record: 2026-07-17

- Installed Homebrew PostgreSQL `16.14`, Google Chrome `150.0.7871.129`, and npm `12.0.1`.
- Rebuilt Python, frontend, and desktop runtimes for macOS/arm64. Previous copied runtimes and the npm 11 desktop retry remain recoverable under `.trash/`.
- Restored the selected SQL snapshot with all expected pre-smoke counts, nine validated foreign keys, the unique viewing-history source-row index, and UTF-8 content.
- Final verification: backend `192` tests passed with `3` destructive PostgreSQL tests skipped; frontend TypeScript/Vite build passed; desktop `7` tests passed.
- Electron rendered recommendations, wishlist, not-interested items, history-backed search results, and live poster responses, then shut down its backend cleanly.
- Google Sheets dry-run read `518` rows with `456` confirmed progress rows and no conflicts. Selenium fetched Douban subject `1292052` with the macOS Chrome path.
- Approved Electron smoke write: recommendation sessions changed `70 -> 71` and recommendation items changed `520 -> 528`; feedback, viewing history, and wishlist counts did not change.
- Deferred: the end-to-end Add watched write test, because no real test movie was selected for the permanent Google Sheets append.
- Limitation: the selected SQL export orders foreign keys before referenced data; this build restored it by reordering only constraint DDL in memory while preserving the source file and hash.
- Limitation: Electron `31.7.7`'s bundled downloader does not settle under Node `26.3.1`; the official macOS arm64 release ZIP was downloaded directly, verified against the package's SHA-256 manifest, and extracted into `desktop/node_modules`.
