import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";

// Next.js(App Router) + TypeScript strict 프론트엔드용 vitest 설정.
// tsconfig.json의 "@/*" alias는 vite-tsconfig-paths가 자동 해석한다.
export default defineConfig({
  plugins: [tsconfigPaths(), react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
  },
});
