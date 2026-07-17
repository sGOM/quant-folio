"use client";

import { StrategyConfig } from "@/lib/api";
import { NumField } from "./fields";

/**
 * 단일종목 전략의 유형별 지표 파라미터 입력 필드.
 * 부모의 2열 그리드 안에 렌더되며, custom/rebalance 는 파라미터가 없어 아무것도 그리지 않는다.
 */
export function IndicatorParamFields({
  config,
  patch,
}: {
  config: StrategyConfig;
  patch: (p: Partial<StrategyConfig>) => void;
}) {
  /** 숫자 파라미터 필드 값(유형별 키)을 읽는다. */
  function num(key: string): number {
    return (config as unknown as Record<string, number>)[key];
  }

  switch (config.type) {
    case "sma_crossover":
    case "ema_crossover":
      return (
        <>
          <NumField label="단기 이평(fast)" min={1} value={num("fast")} onChange={(v) => patch({ fast: v } as Partial<StrategyConfig>)} />
          <NumField label="장기 이평(slow)" min={2} value={num("slow")} onChange={(v) => patch({ slow: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "macd":
      return (
        <>
          <NumField label="fast EMA" min={1} value={num("fast")} onChange={(v) => patch({ fast: v } as Partial<StrategyConfig>)} />
          <NumField label="slow EMA" min={2} value={num("slow")} onChange={(v) => patch({ slow: v } as Partial<StrategyConfig>)} />
          <NumField label="시그널" min={1} value={num("signal")} onChange={(v) => patch({ signal: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "rsi":
      return (
        <>
          <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="과매도(lower)" min={1} max={99} value={num("lower")} onChange={(v) => patch({ lower: v } as Partial<StrategyConfig>)} />
          <NumField label="과매수(upper)" min={1} max={99} value={num("upper")} onChange={(v) => patch({ upper: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "bollinger":
      return (
        <>
          <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="표준편차 배수(σ)" min={0.1} step={0.1} value={num("num_std")} onChange={(v) => patch({ num_std: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "breakout":
      return (
        <NumField label="채널 기간(일)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
      );
    case "momentum":
      return (
        <NumField label="모멘텀 기간(일)" min={1} value={num("lookback")} onChange={(v) => patch({ lookback: v } as Partial<StrategyConfig>)} />
      );
    case "zscore":
      return (
        <>
          <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="진입 임계(σ)" min={0.1} step={0.1} value={num("entry")} onChange={(v) => patch({ entry: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "disparity":
      return (
        <>
          <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="과매도 이격도(lower)" min={1} step={0.1} value={num("lower")} onChange={(v) => patch({ lower: v } as Partial<StrategyConfig>)} />
          <NumField label="과매수 이격도(upper)" min={1} step={0.1} value={num("upper")} onChange={(v) => patch({ upper: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "donchian_squeeze":
      return (
        <>
          <NumField label="기간(period)" min={3} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="BB 배수(bb_mult)" min={0.1} step={0.1} value={num("bb_mult")} onChange={(v) => patch({ bb_mult: v } as Partial<StrategyConfig>)} />
          <NumField label="KC 배수(kc_mult)" min={0.1} step={0.1} value={num("kc_mult")} onChange={(v) => patch({ kc_mult: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "trix":
      return (
        <>
          <NumField label="삼중 EMA 기간" min={1} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="시그널 기간" min={1} value={num("signal_period")} onChange={(v) => patch({ signal_period: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "obv_trend":
      return (
        <NumField label="OBV 이동평균 기간" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
      );
    case "atr_trailing":
      return (
        <>
          <NumField label="채널 기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
          <NumField label="ATR 기간" min={2} value={num("atr_period")} onChange={(v) => patch({ atr_period: v } as Partial<StrategyConfig>)} />
          <NumField label="ATR 배수(k)" min={0.1} step={0.1} value={num("k")} onChange={(v) => patch({ k: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "volatility_breakout":
      return (
        <NumField label="돌파 계수(k)" min={0.1} step={0.1} value={num("k")} onChange={(v) => patch({ k: v } as Partial<StrategyConfig>)} />
      );
    case "keltner":
      return (
        <>
          <NumField label="중심선 EMA 기간" min={2} value={num("ema_period")} onChange={(v) => patch({ ema_period: v } as Partial<StrategyConfig>)} />
          <NumField label="ATR 기간" min={2} value={num("atr_period")} onChange={(v) => patch({ atr_period: v } as Partial<StrategyConfig>)} />
          <NumField label="ATR 배수(mult)" min={0.1} step={0.1} value={num("mult")} onChange={(v) => patch({ mult: v } as Partial<StrategyConfig>)} />
        </>
      );
    case "stochastic":
      return (
        <>
          <NumField label="%K 기간" min={2} value={num("k_period")} onChange={(v) => patch({ k_period: v } as Partial<StrategyConfig>)} />
          <NumField label="%D 기간" min={1} value={num("d_period")} onChange={(v) => patch({ d_period: v } as Partial<StrategyConfig>)} />
          <NumField label="과매도(lower)" min={1} max={99} value={num("lower")} onChange={(v) => patch({ lower: v } as Partial<StrategyConfig>)} />
          <NumField label="과매수(upper)" min={1} max={99} value={num("upper")} onChange={(v) => patch({ upper: v } as Partial<StrategyConfig>)} />
        </>
      );
    default:
      return null;
  }
}
