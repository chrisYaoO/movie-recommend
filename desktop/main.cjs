const { app, BrowserWindow, ipcMain, session } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");
const { ensureBackend } = require("./backend-lifecycle.cjs");
const { posterRequestHeaders } = require("./poster-request-policy.cjs");

const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = Number(process.env.MOVIES_BACKEND_PORT || 8000);
const FRONTEND_PORT = Number(process.env.MOVIES_FRONTEND_PORT || 5173);
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const FRONTEND_DEV_URL = `http://${BACKEND_HOST}:${FRONTEND_PORT}`;

const repoRoot = path.resolve(__dirname, "..");
const frontendRoot = path.join(repoRoot, "frontend");
const frontendDistIndex = path.join(frontendRoot, "dist", "index.html");
const pythonPath = path.join(repoRoot, ".venv", "Scripts", "python.exe");
const runtimeLogPath = path.join(__dirname, "runtime.log");

let backendProcess = null;
let frontendProcess = null;
let mainWindow = null;
let shuttingDown = false;
let backendReadyPromise = null;

function configurePosterRequests() {
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ["https://*.doubanio.com/*"] },
    (details, callback) => {
      callback({ requestHeaders: posterRequestHeaders(details.url, details.requestHeaders) });
    }
  );

  if (process.env.MOVIES_DESKTOP_POSTER_SMOKE_MS) {
    session.defaultSession.webRequest.onCompleted(
      { urls: ["https://*.doubanio.com/*"] },
      (details) => {
        log(`Poster response: ${details.statusCode} ${details.url}`);
      }
    );
  }
}

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`;
  fs.appendFileSync(runtimeLogPath, line, "utf8");
  console.log(message);
}

function spawnProcess(command, args, options) {
  const stdio = fs.openSync(runtimeLogPath, "a");
  return spawn(command, args, {
    cwd: options.cwd,
    env: {
      ...process.env,
      ...options.env
    },
    windowsHide: true,
    stdio: ["ignore", stdio, stdio]
  });
}

function startBackend() {
  if (!fs.existsSync(pythonPath)) {
    throw new Error(`Missing virtualenv Python at ${pythonPath}`);
  }

  log(`Starting backend: ${pythonPath} on ${BACKEND_URL}`);
  backendProcess = spawnProcess(
    pythonPath,
    ["-m", "uvicorn", "backend.app.main:app", "--host", BACKEND_HOST, "--port", String(BACKEND_PORT)],
    {
      cwd: repoRoot,
      env: {
        MOVIES_DESKTOP: "1"
      }
    }
  );

  backendProcess.once("exit", (code, signal) => {
    backendProcess = null;
    if (!shuttingDown) {
      log(`Movies backend exited early: code=${code} signal=${signal}`);
      app.quit();
    }
  });
}

function startFrontendDevServer() {
  const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
  log(`Starting frontend dev server: ${FRONTEND_DEV_URL}`);
  frontendProcess = spawnProcess(
    npmCommand,
    ["run", "dev", "--", "--host", BACKEND_HOST, "--port", String(FRONTEND_PORT)],
    { cwd: frontendRoot, env: {} }
  );

  frontendProcess.once("exit", (code, signal) => {
    frontendProcess = null;
    if (!shuttingDown) {
      log(`Movies frontend dev server exited early: code=${code} signal=${signal}`);
      app.quit();
    }
  });
}

function waitForHttp(url, timeoutMs = 30000) {
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    function attempt() {
      const request = http.get(url, (response) => {
        response.resume();
        if (response.statusCode && response.statusCode < 500) {
          resolve();
          return;
        }
        retry();
      });

      request.on("error", retry);
      request.setTimeout(1000, () => {
        request.destroy();
        retry();
      });
    }

    function retry() {
      if (Date.now() - startedAt > timeoutMs) {
        reject(new Error(`Timed out waiting for ${url}`));
        return;
      }
      setTimeout(attempt, 250);
    }

    attempt();
  });
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 980,
    minHeight: 680,
    title: "Personal Movie Recommender",
    backgroundColor: "#f7f7f4",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    log(`Window failed to load ${validatedURL}: ${errorCode} ${errorDescription}`);
  });

  if (fs.existsSync(frontendDistIndex)) {
    log(`Loading built frontend: ${frontendDistIndex}`);
    await mainWindow.loadFile(frontendDistIndex);
  } else {
    startFrontendDevServer();
    await waitForHttp(FRONTEND_DEV_URL);
    await mainWindow.loadURL(FRONTEND_DEV_URL);
  }

  const smokeExitMs = Number(process.env.MOVIES_DESKTOP_SMOKE_EXIT_MS || 0);
  if (smokeExitMs > 0) {
    const smokeText = await mainWindow.webContents.executeJavaScript(
      "document.body.innerText.trim().slice(0, 200)",
      true
    );
    log(`Smoke DOM text: ${smokeText}`);
    const posterSmokeMs = Number(process.env.MOVIES_DESKTOP_POSTER_SMOKE_MS || 0);
    if (posterSmokeMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, posterSmokeMs));
      const posterState = await mainWindow.webContents.executeJavaScript(
        `JSON.stringify(Array.from(document.querySelectorAll("img.poster-image")).reduce(
          (state, image) => {
            state.total += 1;
            if (image.complete && image.naturalWidth > 0) state.loaded += 1;
            else if (image.complete) state.failed += 1;
            else state.pending += 1;
            return state;
          },
          { total: 0, loaded: 0, failed: 0, pending: 0 }
        ))`,
        true
      );
      log(`Poster smoke state: ${posterState}`);
    }
    setTimeout(() => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.close();
      }
    }, smokeExitMs);
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function stopChildProcess(child) {
  if (!child || child.killed || child.exitCode !== null) return;

  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore"
    });
    return;
  }

  child.kill("SIGTERM");
}

function stopServices() {
  shuttingDown = true;
  stopChildProcess(frontendProcess);
  stopChildProcess(backendProcess);
}

app.whenReady().then(async () => {
  try {
    log(`Desktop app starting from ${repoRoot}`);
    configurePosterRequests();
    backendReadyPromise = ensureBackend({
      backendUrl: BACKEND_URL,
      healthUrl: `${BACKEND_URL}/openapi.json`,
      log,
      startBackend,
      waitForHttp
    }).then(() => {
      log("Backend ready");
      return BACKEND_URL;
    });
    ipcMain.handle("movies:wait-for-backend", () => backendReadyPromise);
    await createWindow();
    await backendReadyPromise;
  } catch (error) {
    log(error.stack || String(error));
    stopServices();
    app.quit();
  }
});

app.on("window-all-closed", () => {
  stopServices();
  app.quit();
});

app.on("before-quit", stopServices);
