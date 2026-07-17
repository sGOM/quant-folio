import { describe, it, expect } from "vitest";
import { alignOverlaySeries, fmtTrackingError } from "@/lib/tracking";

describe("alignOverlaySeries", () => {
  it("백테스트 곡선을 실측 창으로 잘라 재기준한다", () => {
    const realized = [
      { t: "2026-06-01", v: 100 },
      { t: "2026-06-02", v: 102 },
      { t: "2026-06-03", v: 101 },
    ];
    // 백테스트는 훨씬 긴 기간(1월부터)을 갖되, 실측 창(6/1~6/3) 안에도 점이 있다.
    const backtest = [
      { t: "2026-01-01", v: 100 },
      { t: "2026-06-01", v: 150 },
      { t: "2026-06-02", v: 153 },
      { t: "2026-06-03", v: 148 },
      { t: "2026-12-01", v: 200 },
    ];

    const { realized: r, backtest: b } = alignOverlaySeries(realized, backtest);

    expect(r).toEqual(realized);
    // 실측 창 안의 3개 점만 남고, 첫 점(150)이 100으로 재기준된다.
    expect(b).toHaveLength(3);
    expect(b[0]).toEqual({ t: "2026-06-01", v: 100 });
    expect(b[1].v).toBeCloseTo((153 / 150) * 100, 6);
    expect(b[2].v).toBeCloseTo((148 / 150) * 100, 6);
  });

  it("백테스트 곡선이 비어 있으면 backtest=[] 를 반환한다", () => {
    const realized = [
      { t: "2026-06-01", v: 100 },
      { t: "2026-06-02", v: 102 },
    ];
    const { realized: r, backtest: b } = alignOverlaySeries(realized, []);
    expect(r).toEqual(realized);
    expect(b).toEqual([]);
  });

  it("실측 곡선이 비어 있으면 그대로(빈 배열) 반환한다", () => {
    const backtest = [{ t: "2026-06-01", v: 100 }];
    const { realized: r, backtest: b } = alignOverlaySeries([], backtest);
    expect(r).toEqual([]);
    expect(b).toEqual([]);
  });

  it("실측 창과 겹치는 백테스트 구간이 없으면 backtest=[] 를 반환한다", () => {
    const realized = [
      { t: "2026-06-01", v: 100 },
      { t: "2026-06-02", v: 101 },
    ];
    const backtest = [
      { t: "2026-01-01", v: 100 },
      { t: "2026-01-02", v: 105 },
    ];
    const { backtest: b } = alignOverlaySeries(realized, backtest);
    expect(b).toEqual([]);
  });

  it("NaN/Infinity 점은 걸러낸다", () => {
    const realized = [
      { t: "2026-06-01", v: 100 },
      { t: "2026-06-02", v: NaN },
      { t: "2026-06-03", v: 101 },
    ];
    const backtest = [
      { t: "2026-06-01", v: 150 },
      { t: "2026-06-03", v: Infinity },
    ];
    const { realized: r, backtest: b } = alignOverlaySeries(realized, backtest);
    expect(r).toEqual([
      { t: "2026-06-01", v: 100 },
      { t: "2026-06-03", v: 101 },
    ]);
    expect(b).toEqual([{ t: "2026-06-01", v: 100 }]);
  });
});

describe("fmtTrackingError", () => {
  it("소수 비율을 퍼센트 문자열로 변환한다", () => {
    expect(fmtTrackingError(0.0812)).toBe("8.12%");
    expect(fmtTrackingError(0)).toBe("0.00%");
  });

  it("null/undefined 는 '-' 를 반환한다", () => {
    expect(fmtTrackingError(null)).toBe("-");
    expect(fmtTrackingError(undefined)).toBe("-");
  });
});
