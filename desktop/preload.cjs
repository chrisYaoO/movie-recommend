const { contextBridge, ipcRenderer } = require("electron");

const backendPort = Number(process.env.MOVIES_BACKEND_PORT || 8000);

contextBridge.exposeInMainWorld("moviesDesktop", {
  apiBaseUrl: `http://127.0.0.1:${backendPort}`,
  waitForBackend: () => ipcRenderer.invoke("movies:wait-for-backend")
});
