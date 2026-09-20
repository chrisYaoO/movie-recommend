# Desktop Runtime

## Purpose

The Electron shell turns the existing React and FastAPI application into a local desktop window. It does not duplicate business logic. React remains the UI, FastAPI remains the application API, and PostgreSQL plus Google Sheets keep their existing responsibilities.

## Startup

On Windows, `start-app.cmd` is the File Explorer entrypoint. It calls `start-app.ps1`, which verifies dependencies and the built frontend, initializes the schema when `MOVIES_RECOMMENDATION_BACKEND=postgres`, then starts `desktop/launch.cjs`. `start-dev.ps1` performs the same schema initialization before starting the development backend. On macOS, run `npm --prefix desktop start` from the repository root, or use the local `Movies.app` launcher, which starts Homebrew `postgresql@16` first. Both desktop paths use the repository's `.venv` Python.

The schema source is `PostgresViewingHistoryRepository.initialize_schema()` in `backend/app/db/postgres_repository.py`. Run `.\.venv\Scripts\python.exe -m jobs.init_database` to initialize a Windows-local database explicitly. It uses `--dsn`, `MOVIES_POSTGRES_DSN`, or `.env` in that order and is safe to repeat on the current schema. The initializer applies the repository's existing legacy migrations; it does not provide a full comparison against a separately versioned SQL snapshot.

The runtime starts these tasks in parallel:

1. Electron creates the application window and loads `frontend/dist/index.html`, or starts Vite when the built frontend is absent.
2. Electron starts `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on macOS with `-m uvicorn backend.app.main:app`.
3. Frontend API calls wait through the preload IPC bridge until the backend health check succeeds.
4. In desktop mode, FastAPI starts a background Selenium prewarm thread.

The window can render before FastAPI and Selenium are ready. This keeps first paint independent from Python and Chrome startup time.

## Shutdown

Closing the application window:

1. stops the frontend development server when one was used
2. stops FastAPI (`taskkill /T /F` on Windows, `SIGTERM` to the child on macOS)
3. runs FastAPI lifespan cleanup when graceful shutdown is available
4. closes the shared Selenium driver, candidate-queue worker, and PostgreSQL viewing-history connection

Desktop lifecycle smoke checks should verify that Electron, uvicorn, chromedriver, and headless Chrome leave no residual processes.

## Poster Requests

Douban image hosts reject many direct requests without a Douban Referer. Electron applies this request policy only to matching `imgN.doubanio.com` URLs:

```text
Referer: https://movie.douban.com/
```

The React UI keeps stable poster dimensions and shows separate loading and failed-image states.

## Performance

Measured during the July 2026 local build on its dataset and machine:

- React first paint: about 1.5 to 1.8 seconds
- backend readiness: about 2.3 seconds
- recommendation API generation: about 0.16 to 0.19 seconds
- first Selenium prewarm: about 1.8 seconds after browser caches are warm
- reuse of an already prewarmed driver: effectively immediate

Recommendation scoring precomputes the viewing-history content profile once per recommendation run. Google Sheets writes reuse a valid service-account token and refresh once on expiry or a 401 response.

## Configuration

Disable desktop Selenium prewarm for one run:

```powershell
$env:MOVIES_PREWARM_RECORD_SELENIUM="0"
.\start-app.cmd
```

Override the Chrome binary used by record-watched metadata retrieval:

```powershell
$env:MOVIES_RECORD_CHROME_BINARY_PATH="C:\path\to\chrome.exe"
.\start-app.cmd
```

## Moving a Mac database to Windows for testing

For the full Windows native-dependency rebuild, schema audit, current Mac backup restore, and acceptance sequence, see [the Windows local build checklist](checklists/windows-local-build-checklist.md).

First verify a Windows build against an empty local test database. Set its DSN and `MOVIES_RECOMMENDATION_BACKEND=postgres`, run `.\.venv\Scripts\python.exe -m jobs.init_database` twice, then start the API and request `/openapi.json` and `/wishlist`. Both requests should return HTTP 200. This checks repeatable initialization and an application read path without requiring Google Sheets credentials.

After the Windows app builds successfully, use PostgreSQL backup and restore for real-data testing. On the Mac, use a PostgreSQL client compatible with the server and export the authoritative database in custom format (set `MOVIES_POSTGRES_DSN` in the shell from the local configuration without checking credentials into Git):

```bash
mkdir -p data/backups
pg_dump --format=custom --no-owner --no-privileges --file=data/backups/movies-mac.dump "$MOVIES_POSTGRES_DSN"
```

Transfer the dump file securely. On Windows, create a **new empty** database with a distinct name, then restore into it. Run `createdb` and `pg_restore` as the same PostgreSQL role that the Windows app will use; the example assumes that role can create databases:

```powershell
createdb movies_windows_test
pg_restore --no-owner --no-privileges --dbname=movies_windows_test .\data\backups\movies-mac.dump
psql --dbname=movies_windows_test -c "SELECT (SELECT COUNT(*) FROM movies) AS movies, (SELECT COUNT(*) FROM viewing_history) AS viewing_history"
```

Compare the restored row counts with the Mac source. Set Windows `.env` to `MOVIES_RECOMMENDATION_BACKEND=postgres` and `MOVIES_POSTGRES_DSN` for `movies_windows_test`. Run the initializer and start the API before opening Electron:

```powershell
.\.venv\Scripts\python.exe -m jobs.init_database
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

In a second PowerShell window, request `http://127.0.0.1:8000/openapi.json` and `http://127.0.0.1:8000/wishlist`; both should return HTTP 200. Stop the API, then launch `start-app.cmd` and exercise the desktop UI. Viewing-history endpoints also need the existing Google Sheets spreadsheet ID and service-account file. If those credentials are present, API startup can flush pending outbox entries to that Sheet, so use a test Sheet or review pending tasks before a real-data test. A restored backup is a snapshot: changes made on Windows do not flow back to the Mac database. Keep the dump outside the repository or under ignored `data/backups/`; do not commit it.

## Verification

```powershell
Push-Location desktop
npm test
Pop-Location

Push-Location frontend
npm run build
Pop-Location

$env:MOVIES_RECOMMENDATION_BACKEND="memory"
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests
Remove-Item Env:\MOVIES_RECOMMENDATION_BACKEND
```

The unit suite uses the in-memory backend even when local `.env` selects PostgreSQL. PostgreSQL integration tests use a separate test DSN as described in the README.
