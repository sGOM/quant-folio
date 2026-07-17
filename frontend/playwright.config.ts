import { defineConfig, devices } from "@playwright/test";

/**
 * QuantFolio E2E 스모크 설정.
 *
 * 이 테스트는 "가벼운 단위테스트"가 아니라 이미 떠 있는(또는 CI가 띄운) 전체
 * Docker Compose 스택(web/engine/worker/frontend/db/redis/proxy)을 브라우저로
 * 직접 구동한다. 따라서 webServer 자동 기동은 사용하지 않는다 — 앱 기동은
 * `docker compose up -d --build` 로 별도 수행하는 게 전제다(run-quantfolio 스킬 참고).
 *
 * baseURL 은 항상 Caddy 프록시(:8080)를 거친다. web(:8000)·frontend(:3000) 를
 * 직접 찌르면 CORS/쿠키(SameSite)·정적 라우팅이 실제 환경과 달라진다.
 */
export default defineConfig({
  testDir: "./e2e",
  // 백테스트 실행 등 무거운 단계가 섞여 있어 단위테스트보다 타임아웃을 넉넉히 둔다.
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ignoreHTTPSErrors: true,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
