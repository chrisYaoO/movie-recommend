# Mac Codex Build Notes

This note is for a Codex agent continuing this repository on macOS after the full Windows folder is copied over.

The first macOS local build was completed and verified on 2026-07-17. The authoritative execution record is [the macOS local build checklist](checklists/mac-local-build-checklist.md).

## Run The Completed Build

From the repository root:

```bash
brew services start postgresql@16
npm --prefix desktop start
```

The Electron shell starts FastAPI with `.venv/bin/python` and loads the Mac-built `frontend/dist/index.html`. Closing the Electron window stops its backend process. Inspect `desktop/runtime.log` if startup fails.

## Current Shape

This is not a self-contained packaged desktop app yet. It is a local desktop shell around:

- React/Vite frontend in `frontend/`
- Electron shell in `desktop/`
- FastAPI backend in `backend/`
- PostgreSQL as the local application database
- Google Sheets as the viewing-history source of truth for watched-record writes
- Douban cache/import files under `data/`

The platform-specific Python and Chrome paths are now handled in code. A future copy to another Mac must still recreate generated runtime folders rather than reuse Windows artifacts.

## Files That Matter

The migration method is a full folder copy from Windows. Keep these local-only files and directories from that copied folder:

- `.env`
- `.secrets/google-sheets-service-account.json`
- `data/cache/`
- `data/imports/MOVIES.xlsx`
- `.scratch/bandit/latest-model.json`
- `movies-postgres-export-20260712-183619.sql`

Do not rely on these copied runtime folders on macOS:

- `.venv/`
- `frontend/node_modules/`
- `desktop/node_modules/`

They may contain Windows-specific binaries and should be recreated on the Mac.

## macOS Prerequisites

Install:

- Python 3.12 or compatible Python 3.x
- Node.js and npm
- PostgreSQL client and server
- Google Chrome, for Selenium-backed Douban metadata retrieval

Homebrew examples:

```bash
brew install python node postgresql@16
brew install --cask google-chrome
```

Start PostgreSQL according to the local install method. With Homebrew this is usually:

```bash
brew services start postgresql@16
```

## Restore PostgreSQL Data

The application reads recommendations from PostgreSQL. Copying the folder alone is not enough; restore the exported database.

The `movies` database is already restored on this Mac. Do not run the export over it again.

Important: the selected export has a DDL ordering defect. It declares foreign keys before referenced primary keys and before referenced rows are inserted, so a direct `psql -f movies-postgres-export-20260712-183619.sql` fails. The successful build kept the source file and SHA-256 unchanged, reordered only the constraint statements in memory, and restored with `ON_ERROR_STOP` inside the export transaction. See the completed checklist before attempting another restore.

If the `postgres` role or password from the Windows `.env` does not exist on macOS, either create a matching role or update `.env` to the Mac's local DSN.

Recommended macOS `.env` shape:

```text
MOVIES_POSTGRES_DSN=postgresql://<user>:<password>@localhost:5432/movies
MOVIES_RECOMMENDATION_BACKEND=postgres
```

If local PostgreSQL uses peer/trust auth and no password, a DSN like this may be enough:

```text
MOVIES_POSTGRES_DSN=postgresql://<user>@localhost:5432/movies
MOVIES_RECOMMENDATION_BACKEND=postgres
```

## Recreate Dependencies

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Install frontend and desktop dependencies from their lockfiles:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix desktop ci
npm --prefix desktop test
```

Run backend tests from the repo root:

```bash
source .venv/bin/activate
python -m unittest discover -s backend/tests
```

## Desktop Runtime Compatibility

The desktop Python path is platform-aware through `desktop/runtime-paths.cjs`: Windows uses `.venv/Scripts/python.exe` and macOS uses `.venv/bin/python`. Selenium likewise defaults to the installed macOS Chrome executable while retaining the Windows default.

The process-kill path already has a Windows branch and falls back to `SIGTERM` on non-Windows. The frontend dev-server command is also already platform-aware for `npm.cmd` vs `npm`.

There is no packaged macOS launcher. Launch manually from the repository root:

```bash
npm --prefix desktop start
```

Electron `31.7.7` has a clean-install caveat with Node `26.3.1`: its bundled `@electron/get@2.0.3` downloader did not settle, even after npm was updated to `12.0.1`. The completed runtime was installed from the official `electron-v31.7.7-darwin-arm64.zip`, verified against the package manifest SHA-256 `e81b75a185376effcc7dd15aef8877ab48474633e5ac7417810a3b28e694bbfa`. The current `desktop/node_modules` is working; do not discard it casually during local maintenance.

If the desktop path is still being adapted, use browser dev mode instead:

```bash
source .venv/bin/activate
python -m uvicorn backend.app.main:app --reload
```

In another terminal:

```bash
cd frontend
npm run dev
```

Open the Vite URL, usually `http://127.0.0.1:5173`.

## Google Sheets Credentials

Watched-record writes append to Google Sheets before local PostgreSQL persistence. Keep `.secrets/google-sheets-service-account.json` available on macOS.

Verify:

- the JSON exists at `.secrets/google-sheets-service-account.json`
- it contains the expected `spreadsheet_id`
- the Google Sheet is shared with the service account `client_email`
- the Mac system clock is correct

Clock skew can surface as `invalid_grant` from Google auth.

For a non-writing credentials check:

```bash
source .venv/bin/activate
python -m jobs.sync_google_sheets_history --replay-confirmed-progress --dry-run
```

## Data And Cache Expectations

`data/cache/` contains Douban search/detail cache and auto-match progress. It helps avoid re-fetching and preserves matching context, but it is not the primary application database.

`data/imports/MOVIES.xlsx` is a legacy/import input. The current rebuild path treats Google Sheets as the source of truth for viewing history unless the user explicitly says to use local Excel snapshots.

`movies-postgres-export-20260712-183619.sql` is the exported current PostgreSQL data snapshot. Prefer this file for Mac migration over rebuilding from Google Sheets unless the user explicitly wants a fresh rebuild.

## Verification Checklist

After setup:

```bash
source .venv/bin/activate
python -m unittest discover -s backend/tests
```

```bash
cd frontend
npm run build
```

```bash
cd desktop
npm test
```

Then verify PostgreSQL-backed API startup:

```bash
source .venv/bin/activate
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl http://127.0.0.1:8000/openapi.json
```

If recommendations are empty or the app falls back to seed data, check `.env` first. `MOVIES_RECOMMENDATION_BACKEND=postgres` and a working `MOVIES_POSTGRES_DSN` are required for the restored PostgreSQL dataset.

## Things Not To Assume

- Do not assume copied `.venv` works on macOS.
- Do not assume copied `node_modules` works on macOS.
- Do not assume a future clean Electron `npm ci` will download the runtime successfully with the current Electron/Node combination.
- Do not directly replay the selected SQL export; its constraint order must be corrected first.
- Do not rebuild or overwrite PostgreSQL from Google Sheets unless the user asks for a fresh rebuild.
- Do not treat the Electron app as a packaged macOS app; this remains a local development build.

## Completed-Build Attention Items

- PostgreSQL `16.14`, Google Chrome `150.0.7871.129`, and npm `12.0.1` were installed during the build.
- The pre-smoke restored counts matched the SQL snapshot. The approved Electron smoke then changed recommendation sessions from `70` to `71` and recommendation items from `520` to `528`; feedback, viewing history, and wishlist counts did not change.
- The end-to-end Add watched test was not run. It permanently appends to Google Sheets before local PostgreSQL persistence and still has a duplicate-write risk if the local step fails. Choose a real test movie explicitly before running it.
- The Google Sheets dry-run and Selenium/Douban read checks passed. `google-auth[requests]` is required because the code uses the Requests transport.
- npm reported existing dependency vulnerabilities. No `npm audit fix --force` was run because it may introduce breaking upgrades.
- Replaced Windows runtimes and build artifacts remain recoverable under repository-root `.trash/`. Review them before any later cleanup; do not delete source data or secrets.
- Final verification passed: `192` backend tests with `3` destructive PostgreSQL tests skipped, the frontend TypeScript/Vite build, `7` desktop tests, API smoke tests, and real Electron UI checks for recommendations, wishlist, not-interested items, search, posters, and clean shutdown.
