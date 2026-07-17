import { describe, it, expect, vi, afterEach } from "vitest";
import { api, ApiError, API_BASE_URL } from "@/lib/api";

// 실행 환경(NEXT_PUBLIC_API_BASE_URL)에 따라 베이스가 달라질 수 있어 상수 대신 export 값을 사용한다.
const API_BASE = API_BASE_URL;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("getFillQuality: days 는 항상 포함되고 strategy_id 는 지정 시에만 포함된다", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ sample: {}, m1_exec: {}, m2_time: {}, m3_total: {} })),
      );
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.getFillQuality(30);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/backtest/fill-quality?days=30`,
      expect.objectContaining({ credentials: "include" }),
    );

    await api.getFillQuality(60, 23);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/backtest/fill-quality?days=60&strategy_id=23`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("getFillQuality: strategyId 미지정 시 기본 days=90 을 사용한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}));
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.getFillQuality();
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/backtest/fill-quality?days=90`,
      expect.anything(),
    );
  });

  it("applySlippageCalibration: POST 로 URL·body를 구성한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        strategy_id: 23,
        previous_bps: 5,
        applied_bps: 8,
        observed_median_bps: 8,
        sample_size: 40,
        clamped: false,
        reason: "ok",
      }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const result = await api.applySlippageCalibration(23, 8);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/backtest/slippage-calibration/23/apply`,
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ proposed_bps: 8 }),
      }),
    );
    expect(result.applied_bps).toBe(8);
  });

  it("비2xx 응답이면 ApiError 를 던지고 status 를 보존한다(409)", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "제안이 최신 리포트와 일치하지 않습니다." }, 409));
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(api.applySlippageCalibration(23, 8)).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      message: "제안이 최신 리포트와 일치하지 않습니다.",
    });
  });

  it("비2xx 응답이면 ApiError 를 던지고 status 를 보존한다(404)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "전략을 찾을 수 없습니다." }, 404));
    global.fetch = fetchMock as unknown as typeof fetch;

    try {
      await api.getStrategy(999);
      expect.unreachable();
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(404);
      expect((e as ApiError).message).toBe("전략을 찾을 수 없습니다.");
    }
  });

  it("listStrategies: GET 으로 전략 목록을 조회한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([{ id: 1, name: "테스트 전략" }]));
    global.fetch = fetchMock as unknown as typeof fetch;

    const result = await api.listStrategies();
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/strategies`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(result).toEqual([{ id: 1, name: "테스트 전략" }]);
  });

  it("getStrategy: id 로 단건 조회 URL을 구성한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 23, name: "균형 멀티팩터" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.getStrategy(23);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/strategies/23`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("createStrategy: POST 로 name/config/description body 를 구성한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 1 }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const config = {
      type: "sma_crossover" as const,
      symbol: "005930",
      cash: 10_000_000,
      fees: 0.00015,
      tax: 0.002,
      fast: 5,
      slow: 20,
    };
    await api.createStrategy("테스트", config, "설명");
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/strategies`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "테스트", config, description: "설명" }),
      }),
    );
  });

  it("getStrategyTracking: 옵션 없으면 쿼리스트링 없이 호출한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ strategy_id: 23 }));
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.getStrategyTracking(23);
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/strategies/23/tracking`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("getStrategyTracking: dateFrom/dateTo/backtestId 를 쿼리스트링으로 구성한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ strategy_id: 23 }));
    global.fetch = fetchMock as unknown as typeof fetch;

    await api.getStrategyTracking(23, {
      dateFrom: "2026-01-01",
      dateTo: "2026-06-01",
      backtestId: 5,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/strategies/23/tracking?date_from=2026-01-01&date_to=2026-06-01&backtest_id=5`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("getStrategyTracking: 체결 없음(404)이면 ApiError 를 던진다", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "해당 기간 실측 체결이 없습니다." }, 404));
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(api.getStrategyTracking(23)).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
    });
  });

  it("204 응답은 undefined 를 반환한다(deleteStrategy)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const result = await api.deleteStrategy(1);
    expect(result).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/strategies/1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
