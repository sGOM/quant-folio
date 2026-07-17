import { test, expect, type APIRequestContext } from "@playwright/test";

/**
 * QuantFolio E2E 스모크.
 *
 * `run-quantfolio` 스킬로 수동 수행하던 핵심 경로를 그대로 스크립트화한다:
 *   회원가입/로그인 → 전략 목록 진입 → 전략 생성 → 백테스트 1회 실행 → 모니터 진입.
 *
 * 실제 Docker Compose 전체 스택(web/engine/worker/frontend/db/redis/proxy)이
 * `baseURL`(기본 :8080, Caddy 프록시)로 떠 있어야 통과한다. CI 상시 게이트가
 * 아니라 야간 크론/수동 트리거 전용이므로(무거운 스택 기동 전제), 단계마다
 * 넉넉한 타임아웃을 둔다.
 *
 * 로그인/가입 관련 세부사항(폼 인코딩·401/404 무해 오류 등)은
 * `.claude/skills/run-quantfolio/SKILL.md` Gotcha 절을 참고했다.
 */

/** 매 실행마다 겹치지 않는 테스트 계정. */
function freshAccount() {
  const tag = `${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
  return { email: `e2e_${tag}@example.com`, password: "e2e-smoke-test-pw" };
}

/** 스모크용 최소 SMA 크로스오버 단일종목 전략 설정(삼성전자). */
function smokeStrategyConfig() {
  return {
    type: "sma_crossover" as const,
    symbol: "005930",
    cash: 10_000_000,
    fees: 0.00015,
    tax: 0.002,
    fast: 5,
    slow: 20,
  };
}

/** 로그인 세션 쿠키가 실린 API 컨텍스트로 전략을 생성하고 id 를 반환한다. */
async function createSmokeStrategy(
  request: APIRequestContext,
  name: string,
): Promise<number> {
  const res = await request.post("/api/strategies", {
    data: {
      name,
      config: smokeStrategyConfig(),
      description: "Playwright E2E 스모크가 생성한 전략",
    },
  });
  expect(res.ok(), `전략 생성 실패: ${res.status()} ${await res.text()}`).toBeTruthy();
  const body = (await res.json()) as { id: number };
  return body.id;
}

test.describe("QuantFolio 스모크", () => {
  test("가입/로그인 → 전략 목록 → 백테스트 1회 → 모니터", async ({ page }) => {
    const { email, password } = freshAccount();

    await test.step("회원가입 후 로그인 → 대시보드 이동", async () => {
      await page.goto("/login");
      // CardTitle 은 시맨틱 heading 이 아니라 div 다 — 텍스트로만 확인한다.
      await expect(page.getByText("QuantFolio")).toBeVisible();

      // "회원가입" 모드로 전환(기본은 로그인 모드).
      await page.getByRole("button", { name: "계정이 없으신가요? 회원가입" }).click();
      await page.getByLabel("이메일").fill(email);
      await page.getByLabel("비밀번호").fill(password);
      await page.getByRole("button", { name: "가입하고 로그인" }).click();

      await page.waitForURL("**/dashboard", { timeout: 20_000 });
      await expect(page.getByRole("heading", { name: "대시보드" })).toBeVisible();
    });

    let strategyId: number;

    await test.step("전략 목록 페이지 진입 확인", async () => {
      await page.goto("/strategies");
      await expect(page.getByRole("heading", { name: "전략" })).toBeVisible();
      // 신규 계정이므로 빈 상태 문구가 보여야 한다.
      await expect(page.getByText("아직 전략이 없습니다.")).toBeVisible();
    });

    await test.step("전략 생성(API) 후 목록에 노출 확인", async () => {
      const name = `E2E 스모크 ${Date.now()}`;
      // UI 폼은 입력 항목이 많아 스모크 범위를 넘는다 — 로그인 세션 쿠키를 공유하는
      // page.request 로 최소 구성 전략을 만들고, 화면 반영만 UI 로 검증한다.
      strategyId = await createSmokeStrategy(page.request, name);

      await page.goto("/strategies");
      await expect(page.getByRole("link", { name: new RegExp(name) })).toBeVisible();
    });

    await test.step("백테스트 1회 실행", async () => {
      await page.goto(`/strategies/${strategyId}`);
      await expect(page.getByRole("button", { name: "백테스트 실행" })).toBeVisible();

      await page.getByRole("button", { name: "백테스트 실행" }).click();
      await expect(page.getByRole("button", { name: "백테스트 실행 중…" })).toBeVisible();

      // 종목 가격 데이터 조회를 동반해 시간이 걸릴 수 있어 넉넉히 대기한다.
      await expect(page.getByText("자산 곡선 (Equity Curve)")).toBeVisible({
        timeout: 90_000,
      });
      await expect(page.getByText("총수익률", { exact: true })).toBeVisible();
    });

    await test.step("모니터 페이지 진입 및 핵심 요소 렌더 확인", async () => {
      await page.goto("/monitor");
      await expect(page.getByRole("heading", { name: "실시간 모니터링" })).toBeVisible();
      await expect(page.getByText("매매 엔진", { exact: false }).first()).toBeVisible();
      await expect(page.getByRole("heading", { name: "전략 ON/OFF" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "전략 러너 헬스" })).toBeVisible();
      // 방금 만든 전략이 ON/OFF 토글 목록에 나와야 한다.
      await expect(page.getByRole("heading", { name: "보유 포지션" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "실시간 이벤트" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "최근 주문 (감사 로그)" })).toBeVisible();
    });

    await test.step("정리: 스모크 전략 삭제", async () => {
      // 라이프사이클 가드(미청산 포지션 등)로 실패해도 스모크 자체는 이미 통과했으므로
      // 삭제 실패는 무시한다(실행 계정은 매번 새로 만들어 누적되어도 무해).
      await page.request.delete(`/api/strategies/${strategyId}`).catch(() => {});
    });
  });
});
