/**
 * 전략 실측(모의투자/라이브) ↔ 백테스트 기대곡선 오버레이(improvements.md §6) 매핑 유틸.
 *
 * 백엔드가 돌려주는 realized_index/backtest_index 는 각자 "자기 곡선의 첫 값" 기준 100
 * 으로 재기준된다(app/services/backtest/tracking.py `_to_index100`). 문제는 백테스트
 * 곡선이 보통 실측보다 훨씬 긴 과거 기간을 담고 있어, 두 곡선을 그대로 겹치면 실측
 * 구간이 오른쪽 끝에 짧게 눌려 비교가 되지 않는다는 점이다.
 *
 * 이 모듈은 화면 오버레이 전용으로 백테스트 곡선을 실측 곡선의 날짜 창으로 잘라내고,
 * 그 창의 시작점을 다시 100 으로 재기준한다 — "실측이 시작된 시점부터 두 곡선이 같은
 * 출발선에서 얼마나 벌어졌는가"를 보여주기 위함이다. 지표(tracking_error 등)는 서버가
 * 이미 공통 거래일 기준으로 계산해 내려주므로 여기서는 건드리지 않는다.
 */

export interface CurvePoint {
  t: string;
  v: number;
}

/**
 * 백테스트 인덱스 곡선을 실측 인덱스 곡선의 날짜 창 [min(t), max(t)] 로 잘라내고,
 * 그 구간 첫 점을 100 으로 재기준한다.
 *
 * @param realizedIndex 실측 곡선(100 기준 정규화, 서버 응답 그대로)
 * @param backtestIndex 백테스트 곡선(100 기준 정규화, 서버 응답 그대로)
 * @returns 오버레이용 { realized, backtest } — realized 는 입력 그대로, backtest 는
 *   실측 창으로 잘라 재기준한 결과(창과 겹치는 점이 없으면 빈 배열).
 */
export function alignOverlaySeries(
  realizedIndex: CurvePoint[],
  backtestIndex: CurvePoint[],
): { realized: CurvePoint[]; backtest: CurvePoint[] } {
  const realized = realizedIndex.filter((p) => Number.isFinite(p.v));
  if (realized.length === 0 || backtestIndex.length === 0) {
    return { realized, backtest: [] };
  }

  const from = realized[0].t;
  const to = realized[realized.length - 1].t;
  const windowed = backtestIndex.filter(
    (p) => Number.isFinite(p.v) && p.t >= from && p.t <= to,
  );
  if (windowed.length === 0) {
    return { realized, backtest: [] };
  }

  const base = windowed[0].v;
  if (!(base > 0)) {
    return { realized, backtest: [] };
  }
  const backtest = windowed.map((p) => ({ t: p.t, v: (p.v / base) * 100 }));
  return { realized, backtest };
}

/** 연율화 트래킹에러(소수, 0.08=8%/yr)를 퍼센트 문자열로. null → "-". */
export function fmtTrackingError(v: number | null | undefined): string {
  return v == null ? "-" : `${(v * 100).toFixed(2)}%`;
}
