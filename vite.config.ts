import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", rewrite: (path) => path.replace(/^\/api/, "") } },
  },
  test: { environment: "jsdom", setupFiles: ["./frontend/src/test/setup.ts"] },
  build: {
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
