# Desktop Runtime

## Purpose

The Electron shell turns the existing React and FastAPI application into a local desktop window. It does not duplicate business logic. React remains the UI, FastAPI remains the application API, and PostgreSQL plus Google Sheets keep their existing responsibilities.

## Startup

`start-app.cmd` is the File Explorer entrypoint. It calls `start-app.ps1`, which verifies dependencies and the built frontend, then starts `desktop/launch.cjs`.

The runtime starts these tasks in parallel:

1. Electron creates the application window and loads `frontend/dist/index.html`.
2. Electron starts `.venv\Scripts\python.exe -m uvicorn backend.app.main:app`.
3. Frontend API calls wait through the preload IPC bridge until the backend health check succeeds.
4. In desktop mode, FastAPI starts a background Selenium prewarm thread.

The window can render before FastAPI and Selenium are ready. This keeps first paint independent from Python and Chrome startup time.

## Shutdown

Closing the application window:

1. stops the frontend development server when one was used
2. terminates the FastAPI process tree
3. runs FastAPI lifespan cleanup when graceful shutdown is available
4. closes the shared Selenium driver and PostgreSQL viewing-history connection

Desktop lifecycle smoke checks should verify that Electron, uvicorn, chromedriver, and headless Chrome leave no residual processes.

## Poster Requests

Douban image hosts reject many direct requests without a Douban Referer. Electron applies this request policy only to matching `imgN.doubanio.com` URLs:

```text
Referer: https://movie.douban.com/
```

The React UI keeps stable poster dimensions and shows separate loading and failed-image states.

## Performance

Measured on the current local dataset and machine:

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

## Verification

```powershell
Push-Location desktop
npm test
Pop-Location

Push-Location frontend
npm run build
Pop-Location

.\.venv\Scripts\python.exe -m unittest discover -s backend\tests
```
