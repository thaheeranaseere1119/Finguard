import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the FastAPI backend (API_TARGET, default http://localhost:8010).
export default defineConfig({
  plugins: [react()],
  build: { rollupOptions: { output: { manualChunks: { charts: ["recharts"], vendor: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"] } } } },
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.API_TARGET ?? "http://localhost:8010", changeOrigin: false } },
  },
});
