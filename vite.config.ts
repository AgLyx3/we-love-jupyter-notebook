import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: `http://127.0.0.1:${process.env.BACKEND_PORT ?? "8000"}`, rewrite: (path) => path.replace(/^\/api/, "") } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./frontend/src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**", "dist/**", "backend/app/web/**", ".claude/**"],
  },
  build: {
    // Inside the Python package, not a sibling `dist/`, so the built frontend
    // travels with the wheel: a `dist/` at the repo root is not part of any
    // package and ships nowhere. `backend/app/web` is declared as package data
    // in pyproject.toml, and it is where `backend/app/bundled.py` looks first.
    // Only ever build output — emptied on each build.
    outDir: "backend/app/web",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("@codemirror") || id.includes("@uiw/react-codemirror")) return "editor";
          if (id.includes("react-markdown") || id.includes("remark-") || id.includes("micromark")) return "markdown";
          if (id.includes("lucide-react")) return "icons";
          if (id.includes("node_modules/react") || id.includes("node_modules/scheduler")) return "react";
        },
      },
    },
  },
});
