import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/movies": "http://127.0.0.1:8000",
      "/viewing-history": "http://127.0.0.1:8000",
      "/recommendations": "http://127.0.0.1:8000",
      "/wishlist": "http://127.0.0.1:8000",
      "/not-interested": "http://127.0.0.1:8000"
    }
  }
});
