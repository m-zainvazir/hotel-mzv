import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

// Library mode, single IIFE: the embed contract is one <script src="…">
// tag with no other build step for the client site, so everything —
// Preact, the CSS, all of it — bundles into one file. No code splitting
// (a second network request the embed contract doesn't have room for),
// no sourcemap (nothing outside this repo needs to debug the minified
// build, and it keeps dist/ — which is committed, see .gitignore — small).
export default defineConfig({
  plugins: [preact()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    lib: {
      entry: "src/main.ts",
      name: "AIReceptionistWidget",
      formats: ["iife"],
      fileName: () => "widget.js",
    },
  },
});
