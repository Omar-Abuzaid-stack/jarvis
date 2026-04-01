import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5180,
    proxy: {
      "/ws": {
        target: "ws://localhost:8340",
        ws: true,
      },
      "/api": {
        target: "http://localhost:8340",
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
