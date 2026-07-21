// 전략 폼 입력 검증 — 순수 함수만 모아 폼 컴포넌트와 분리(단위 테스트 가능).

import { Operand, StrategyConfig } from "@/lib/api";
import { flattenConditions } from "@/lib/strategy";

/** method="score" 전환 시 기본 팩터 가중치(metrics 화면과 동일한 0.4/0.3/0.3, quality=growth=0). */
export const DEFAULT_FACTOR_WEIGHTS = {
  momentum: 0.4,
  value: 0.3,
  lowvol: 0.3,
  quality: 0.0,
  growth: 0.0,
};

export type FactorWeights = typeof DEFAULT_FACTOR_WEIGHTS;

/** custom 피연산자의 필수 필드 검증. @returns 오류 메시지 또는 null */
export function validateOperand(o: Operand): string | null {
  switch (o.kind) {
    case "const":
      if (o.value === null || o.value === undefined || !Number.isFinite(o.value))
        return "상수 값을 입력하세요.";
      break;
    case "sma":
    case "ema":
    case "rsi":
      if (!o.period || o.period < 1) return "지표 기간을 1 이상으로 입력하세요.";
      break;
    case "macd_line":
    case "macd_signal":
      if (!o.fast || !o.slow || !o.signal)
        return "MACD fast/slow/signal 을 모두 입력하세요.";
      if (o.fast >= o.slow) return "MACD fast 는 slow 보다 작아야 합니다.";
      break;
  }
  return null;
}

/** custom 전략이 종가 외 OHLC/거래량 source 를 참조하는지. */
export function customUsesOhlc(config: StrategyConfig): boolean {
  if (config.type !== "custom") return false;
  const conds = [
    ...flattenConditions(config.entry),
    ...flattenConditions(config.exit),
  ];
  return conds.some((c) =>
    [c.left, c.right].some(
      (o) => o.kind === "price" && o.source != null && o.source !== "close",
    ),
  );
}

/** 리밸런싱 전략 전용 검증(universe·선정·자본·실행 시각). */
function validateRebalance(config: Extract<StrategyConfig, { type: "rebalance" }>): string | null {
  const rule = config.selection.universe_rule;
  const isPit = rule != null && rule.source !== "fixed";
  // PIT 소스는 지수 구성종목을 자동 후보풀로 쓰므로 universe 를 비워도 된다.
  if (!isPit && config.universe.length < 1) return "리밸런싱 종목을 1개 이상 추가하세요.";
  if (config.universe.some((s) => !s.trim())) return "빈 종목코드가 있습니다.";
  if (new Set(config.universe).size !== config.universe.length)
    return "중복 종목코드가 있습니다.";
  const isSelect = config.selection.method === "momentum" || config.selection.method === "score";
  if (isPit) {
    // PIT + 상대강도 축소(pick) 사용 시 top_n ≤ pick 이어야 한다.
    if (isSelect && rule.type === "momentum" && rule.pick != null && config.selection.top_n > rule.pick)
      return "선정 종목 수(top_n)는 후보 종목 수(pick) 이하여야 합니다.";
  } else if (isSelect && config.selection.top_n > config.universe.length) {
    return "선정 종목 수(top_n)는 종목 수 이하여야 합니다.";
  }
  if (config.selection.method === "score") {
    const fw = config.selection.factor_weights ?? DEFAULT_FACTOR_WEIGHTS;
    const sum = fw.momentum + fw.value + fw.lowvol + fw.quality + fw.growth;
    if (Math.abs(sum - 1) > 1e-6)
      return `팩터 가중치 합은 1.00 이어야 합니다(현재 ${sum.toFixed(2)}).`;
  }
  if (config.weighting === "score" && config.selection.method !== "score")
    return "'점수 순위 가중'은 선정 방식이 멀티팩터일 때만 사용할 수 있습니다.";
  const rl = config.risk_layer;
  if (rl?.max_position_pct != null && rl.max_position_pct <= config.drift_band_pct)
    return "종목 집중 한도는 드리프트 밴드보다 커야 합니다(작으면 진입이 발생하지 않습니다).";
  if (!Number.isFinite(config.capital) || config.capital <= 0)
    return "배정 자본은 0보다 커야 합니다.";
  if (!/^\d{1,2}:\d{2}$/.test(config.rebalance_time))
    return "실행 시각은 HH:MM 형식이어야 합니다.";
  if (!Number.isFinite(config.fees) || config.fees < 0 || config.fees > 0.01)
    return "위탁수수료는 0~1% 사이여야 합니다.";
  if (!Number.isFinite(config.tax) || config.tax < 0 || config.tax > 0.01)
    return "증권거래세는 0~1% 사이여야 합니다.";
  return null;
}

/** 유형별 파라미터(이평 기간·임계 등) 검증. */
function validateTypeParams(config: StrategyConfig): string | null {
  switch (config.type) {
    case "sma_crossover":
    case "ema_crossover":
      if (config.fast < 1 || config.slow < 2)
        return "이평 기간이 유효하지 않습니다.";
      if (config.fast >= config.slow)
        return "단기 이평(fast)은 장기 이평(slow)보다 작아야 합니다.";
      break;
    case "macd":
      if (config.fast >= config.slow)
        return "MACD fast 는 slow 보다 작아야 합니다.";
      if (config.signal < 1) return "시그널 기간이 유효하지 않습니다.";
      break;
    case "rsi":
      if (config.period < 2) return "RSI 기간은 2 이상이어야 합니다.";
      if (config.lower >= config.upper)
        return "RSI 하한(lower)은 상한(upper)보다 작아야 합니다.";
      break;
    case "bollinger":
      if (config.period < 2) return "기간은 2 이상이어야 합니다.";
      if (config.num_std <= 0) return "표준편차 배수는 0보다 커야 합니다.";
      break;
    case "breakout":
      if (config.period < 2) return "채널 기간은 2 이상이어야 합니다.";
      break;
    case "momentum":
      if (config.lookback < 1) return "모멘텀 기간은 1 이상이어야 합니다.";
      break;
    case "zscore":
      if (config.period < 2) return "기간은 2 이상이어야 합니다.";
      if (config.entry <= 0) return "진입 z 임계는 0보다 커야 합니다.";
      break;
    case "disparity":
      if (config.period < 2) return "기간은 2 이상이어야 합니다.";
      if (config.lower >= config.upper)
        return "이격도 하한(lower)은 상한(upper)보다 작아야 합니다.";
      break;
    case "donchian_squeeze":
      if (config.period < 3) return "기간은 3 이상이어야 합니다.";
      if (config.bb_mult <= 0 || config.kc_mult <= 0)
        return "BB/KC 배수는 0보다 커야 합니다.";
      break;
    case "trix":
      if (config.period < 1 || config.signal_period < 1)
        return "TRIX 기간/시그널 기간은 1 이상이어야 합니다.";
      break;
    case "obv_trend":
      if (config.period < 2) return "OBV 이동평균 기간은 2 이상이어야 합니다.";
      break;
    case "atr_trailing":
      if (config.period < 2 || config.atr_period < 2)
        return "채널/ATR 기간은 2 이상이어야 합니다.";
      if (config.k <= 0) return "ATR 배수(k)는 0보다 커야 합니다.";
      break;
    case "volatility_breakout":
      if (config.k <= 0) return "돌파 계수(k)는 0보다 커야 합니다.";
      break;
    case "keltner":
      if (config.ema_period < 2 || config.atr_period < 2)
        return "EMA/ATR 기간은 2 이상이어야 합니다.";
      if (config.mult <= 0) return "ATR 배수(mult)는 0보다 커야 합니다.";
      break;
    case "stochastic":
      if (config.k_period < 2 || config.d_period < 1)
        return "%K/%D 기간이 유효하지 않습니다.";
      if (config.lower >= config.upper)
        return "스토캐스틱 하한(lower)은 상한(upper)보다 작아야 합니다.";
      break;
    case "custom": {
      const entryConds = flattenConditions(config.entry);
      const exitConds = flattenConditions(config.exit);
      if (entryConds.length < 1) return "진입 조건을 1개 이상 추가하세요.";
      if (exitConds.length < 1) return "청산 조건을 1개 이상 추가하세요.";
      for (const c of [...entryConds, ...exitConds]) {
        const err = validateOperand(c.left) ?? validateOperand(c.right);
        if (err) return err;
      }
      break;
    }
  }
  return null;
}

/**
 * 전략 폼 전체 입력 검증(제출 전 호출).
 * @returns 첫 오류 메시지, 문제 없으면 null
 */
export function validateStrategyForm(name: string, config: StrategyConfig): string | null {
  if (!name.trim()) return "전략 이름을 입력하세요.";

  // 리밸런싱은 단일종목 전략과 검증 항목이 다르다(universe·선정·자본).
  if (config.type === "rebalance") return validateRebalance(config);

  if (!config.symbol.trim()) return "종목코드를 입력하세요.";
  if (!Number.isFinite(config.cash) || config.cash <= 0)
    return "초기자본은 0보다 커야 합니다.";
  if (!Number.isFinite(config.fees) || config.fees < 0 || config.fees > 0.01)
    return "위탁수수료는 0~1% 사이여야 합니다.";
  if (!Number.isFinite(config.tax) || config.tax < 0 || config.tax > 0.01)
    return "증권거래세는 0~1% 사이여야 합니다.";

  const paramErr = validateTypeParams(config);
  if (paramErr) return paramErr;

  for (const k of ["stop_loss_pct", "take_profit_pct", "trailing_stop_pct"] as const) {
    const v = config[k];
    if (v !== null && v !== undefined && (v <= 0 || v > 1))
      return "손절/익절/트레일링 비율은 0 초과 100% 이하여야 합니다.";
  }
  return null;
}
