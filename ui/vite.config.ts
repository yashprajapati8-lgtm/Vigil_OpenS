import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Same routes as the production nginx, so `npm run dev` behaves like the container.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/store": { target: "http://localhost:8001", rewrite: (p) => p.replace(/^\/api\/store/, "") },
      "/api": { target: "http://localhost:8080" },
      "^/proving-ground/": { target: "http://localhost:8080" },  // the data, not the page
      "/chaos": { target: "http://localhost:8090", rewrite: (p) => p.replace(/^\/chaos/, "") },
    },
  },
});
