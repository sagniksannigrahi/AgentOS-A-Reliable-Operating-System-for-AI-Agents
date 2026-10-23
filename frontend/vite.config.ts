import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the dashboard proxies API calls to the backend so the browser sees one origin.
const target = process.env.AGENTOS_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/health": target,
      "/metrics": target,
    },
  },
});
