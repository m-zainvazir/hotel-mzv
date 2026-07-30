import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

// Plain SPA build, NOT library mode: unlike widget/, this isn't an embed
// contract — nothing pastes a <script> tag pointing at this bundle. Hashed
// asset filenames + one index.html entry, served from /admin/assets
// (app/main.py's guarded StaticFiles mount), with a catch-all route falling
// back to /admin/index.html for any deep link (e.g. /admin/#/tenants/foo).
export default defineConfig({
  base: "/admin/",
  plugins: [preact()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
