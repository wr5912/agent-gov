import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_DEV_PROXY_TARGET || "http://localhost:58080",
        changeOrigin: true,
      },
      "/health": {
        target: process.env.VITE_DEV_PROXY_TARGET || "http://localhost:58080",
        changeOrigin: true,
      },
      "/v1": {
        target: process.env.VITE_DEV_PROXY_TARGET || "http://localhost:58080",
        changeOrigin: true,
      },
    },
  },
});
