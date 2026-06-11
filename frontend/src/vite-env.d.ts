/// <reference types="vite/client" />

interface Window {
  moviesDesktop?: {
    apiBaseUrl: string;
    waitForBackend: () => Promise<string>;
  };
}
