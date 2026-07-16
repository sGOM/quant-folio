"use client";

import { useState } from "react";
import { ConditionGroup, Operand, RebalanceConfig, StrategyConfig, StrategyType, UniverseRule } from "@/lib/api";
import {
  STRATEGY_TYPES,
  STRATEGY_TYPE_LABELS,
  STRATEGY_INFO,
  OHLC_STRATEGY_TYPES,
  defaultConfig,
  formatCustomFormula,
  flattenConditions,
} from "@/lib/strategy";
import { RuleBuilder } from "@/components/RuleBuilder";
import { SymbolSearch } from "@/components/SymbolSearch";
import { Button } from "@/components/ui/button";

/**
 * 입력/셀렉트 공용 스타일(shadcn input 토큰). 자식 컴포넌트에도 동일 톤을 적용한다.
 * bg-transparent 대신 bg-input 을 명시해 네이티브 select 가 OS 기본(흰 배경)
 * 콤보박스로 렌더되는 것을 방지하고, text-foreground 로 글씨 대비를 보장한다.
 */
const INPUT =
  "flex h-9 w-full rounded-md border border-input bg-input text-foreground px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

/**
 * 전략 생성·편집 공용 폼. 유형 선택에 따라 파라미터 입력 필드를 동적으로 렌더하고,
 * 공통 필드(종목·초기자본·손절/익절/트레일링)를 함께 입력받는다.
 *
 * @param initialName        초기 이름(편집 시)
 * @param initialDescription 초기 설명(편집 시)
 * @param initialConfig      초기 설정(편집 시). 없으면 SMA 기본값으로 시작
 * @param submitLabel        제출 버튼 라벨
 * @param pending            제출 진행 중 여부(버튼 비활성)
 * @param error              서버/제출 에러 메시지
 * @param onSubmit           검증 통과 시 (name, config, description) 전달
 * @param onCancel           취소 버튼(있으면 렌더)
 */
export function StrategyForm({
  initialName = "",
  initialDescription = "",
  initialConfig,
  submitLabel,
  pending,
  error,
  onSubmit,
  onCancel,
}: {
  initialName?: string;
  initialDescription?: string;
  initialConfig?: StrategyConfig;
  submitLabel: string;
  pending?: boolean;
  error?: string | null;
  onSubmit: (name: string, config: StrategyConfig, description: string) => void;
  onCancel?: () => void;
}) {
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription);
  const [config, setConfig] = useState<StrategyConfig>(
    // 편집 시 거래비용·체결 필드가 없는 레거시 설정은 기본값으로 채운다(없는 키만 보강).
    initialConfig
      ? ({
          ...initialConfig,
          fees: initialConfig.fees ?? 0.00015,
          tax: initialConfig.tax ?? 0.002,
          fill_mode: initialConfig.fill_mode ?? "next_close",
          slippage_bps: initialConfig.slippage_bps ?? 5,
          slippage_vol_scale: initialConfig.slippage_vol_scale ?? 0,
          risk_free_rate: initialConfig.risk_free_rate ?? 0,
        } as StrategyConfig)
      : defaultConfig("sma_crossover"),
  );
  const [formError, setFormError] = useState<string | null>(null);

  /** 유형 변경 시 공통 필드는 유지하고 유형별 파라미터만 기본값으로 교체한다. */
  function changeType(type: StrategyType) {
    // 리밸런싱은 공통 필드 형태가 달라 보존 없이 기본값으로 전환한다.
    if (type === "rebalance" || config.type === "rebalance") {
      setConfig(defaultConfig(type));
      return;
    }
    const {
      symbol, cash, fees, tax, stop_loss_pct, take_profit_pct, trailing_stop_pct,
      fill_mode, slippage_bps, slippage_vol_scale, risk_free_rate,
    } = config;
    setConfig(
      defaultConfig(type, {
        symbol,
        cash,
        fees: fees ?? 0.00015,
        tax: tax ?? 0.002,
        stop_loss_pct,
        take_profit_pct,
        trailing_stop_pct,
        fill_mode: fill_mode ?? "next_close",
        slippage_bps: slippage_bps ?? 5,
        slippage_vol_scale: slippage_vol_scale ?? 0,
        risk_free_rate: risk_free_rate ?? 0,
      }),
    );
  }

  /** config 의 일부 필드를 병합 갱신한다. */
  function patch(p: Partial<StrategyConfig>) {
    setConfig((c) => ({ ...c, ...p }) as StrategyConfig);
  }

  /** 숫자 파라미터 필드 값(유형별 키)을 읽는다. */
  function num(key: string): number {
    return (config as unknown as Record<string, number>)[key];
  }

  /** 리스크 비율(0~1)을 퍼센트 입력값으로(없으면 ""). */
  function pctValue(key: "stop_loss_pct" | "take_profit_pct" | "trailing_stop_pct") {
    if (config.type === "rebalance") return "";
    const v = config[key];
    return v === null || v === undefined ? "" : String(v * 100);
  }

  /** 거래비용(fees/tax) 비율(0~1)을 퍼센트 입력값으로. 빈 값이면 "". */
  function costValue(key: "fees" | "tax"): string {
    const v = (config as unknown as Record<string, number>)[key];
    if (v === null || v === undefined || !Number.isFinite(v)) return "";
    // 부동소수 오차 제거(예: 0.00015*100 = 0.015).
    return String(Number((v * 100).toFixed(4)));
  }

  /** 퍼센트 입력 → 비율(0~1) 저장. 빈 값이면 0(비용 없음). */
  function setCost(key: "fees" | "tax", raw: string) {
    if (raw.trim() === "") {
      patch({ [key]: 0 } as Partial<StrategyConfig>);
      return;
    }
    const n = Number(raw);
    patch({ [key]: Number.isFinite(n) ? n / 100 : 0 } as Partial<StrategyConfig>);
  }

  /** 퍼센트 입력 → 비율(0~1) 저장. 빈 값이면 null(비활성). */
  function setPct(
    key: "stop_loss_pct" | "take_profit_pct" | "trailing_stop_pct",
    raw: string,
  ) {
    if (config.type === "rebalance") return;
    if (raw.trim() === "") {
      patch({ [key]: null } as Partial<StrategyConfig>);
      return;
    }
    const n = Number(raw);
    patch({ [key]: Number.isFinite(n) ? n / 100 : null } as Partial<StrategyConfig>);
  }

  /** 입력값 검증. @returns 첫 오류 메시지, 없으면 null */
  function validate(): string | null {
    if (!name.trim()) return "전략 이름을 입력하세요.";

    // 리밸런싱은 단일종목 전략과 검증 항목이 다르다(universe·선정·자본).
    if (config.type === "rebalance") {
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
      return null;
    }

    if (!config.symbol.trim()) return "종목코드를 입력하세요.";
    if (!Number.isFinite(config.cash) || config.cash <= 0)
      return "초기자본은 0보다 커야 합니다.";
    if (!Number.isFinite(config.fees) || config.fees < 0 || config.fees > 0.01)
      return "위탁수수료는 0~1% 사이여야 합니다.";
    if (!Number.isFinite(config.tax) || config.tax < 0 || config.tax > 0.01)
      return "증권거래세는 0~1% 사이여야 합니다.";

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

    for (const k of [
      "stop_loss_pct",
      "take_profit_pct",
      "trailing_stop_pct",
    ] as const) {
      const v = config[k];
      if (v !== null && v !== undefined && (v <= 0 || v > 1))
        return "손절/익절/트레일링 비율은 0 초과 100% 이하여야 합니다.";
    }
    return null;
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const err = validate();
    setFormError(err);
    if (!err) onSubmit(name.trim(), config, description.trim());
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 space-y-3 rounded-lg border bg-card p-4"
    >
      <Field label="전략 이름">
        <input
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="삼성전자 골든크로스"
          className={INPUT}
        />
      </Field>

      <Field label="전략 설명 (선택)">
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="전략의 아이디어·근거·사용법을 적어 두면 공유 시 함께 표시됩니다."
          rows={2}
          maxLength={2000}
          className={`${INPUT} h-auto min-h-[60px] resize-y py-2`}
        />
      </Field>

      <Field label="전략 유형">
        <select
          value={config.type}
          onChange={(e) => changeType(e.target.value as StrategyType)}
          className={INPUT}
        >
          {STRATEGY_TYPES.map((t) => (
            <option key={t} value={t}>
              {STRATEGY_TYPE_LABELS[t]}
            </option>
          ))}
        </select>
      </Field>

      <AlgorithmInfo config={config} />

      {config.type === "rebalance" && (
        <RebalanceFields config={config} patch={patch} />
      )}

      {config.type !== "rebalance" && (
      <div className="grid grid-cols-2 gap-3">
        <Field label="종목코드">
          <SymbolSearch
            value={config.symbol}
            onChange={(code) => patch({ symbol: code })}
          />
        </Field>
        <Field label="초기자본(원)">
          <input
            type="number"
            min={1}
            value={config.cash}
            onChange={(e) => patch({ cash: Number(e.target.value) })}
            className={INPUT}
          />
        </Field>

        {/* 유형별 파라미터 */}
        {(config.type === "sma_crossover" || config.type === "ema_crossover") && (
          <>
            <NumField label="단기 이평(fast)" min={1} value={num("fast")} onChange={(v) => patch({ fast: v } as Partial<StrategyConfig>)} />
            <NumField label="장기 이평(slow)" min={2} value={num("slow")} onChange={(v) => patch({ slow: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "macd" && (
          <>
            <NumField label="fast EMA" min={1} value={num("fast")} onChange={(v) => patch({ fast: v } as Partial<StrategyConfig>)} />
            <NumField label="slow EMA" min={2} value={num("slow")} onChange={(v) => patch({ slow: v } as Partial<StrategyConfig>)} />
            <NumField label="시그널" min={1} value={num("signal")} onChange={(v) => patch({ signal: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "rsi" && (
          <>
            <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="과매도(lower)" min={1} max={99} value={num("lower")} onChange={(v) => patch({ lower: v } as Partial<StrategyConfig>)} />
            <NumField label="과매수(upper)" min={1} max={99} value={num("upper")} onChange={(v) => patch({ upper: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "bollinger" && (
          <>
            <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="표준편차 배수(σ)" min={0.1} step={0.1} value={num("num_std")} onChange={(v) => patch({ num_std: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "breakout" && (
          <NumField label="채널 기간(일)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
        )}
        {config.type === "momentum" && (
          <NumField label="모멘텀 기간(일)" min={1} value={num("lookback")} onChange={(v) => patch({ lookback: v } as Partial<StrategyConfig>)} />
        )}
        {config.type === "zscore" && (
          <>
            <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="진입 임계(σ)" min={0.1} step={0.1} value={num("entry")} onChange={(v) => patch({ entry: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "disparity" && (
          <>
            <NumField label="기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="과매도 이격도(lower)" min={1} step={0.1} value={num("lower")} onChange={(v) => patch({ lower: v } as Partial<StrategyConfig>)} />
            <NumField label="과매수 이격도(upper)" min={1} step={0.1} value={num("upper")} onChange={(v) => patch({ upper: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "donchian_squeeze" && (
          <>
            <NumField label="기간(period)" min={3} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="BB 배수(bb_mult)" min={0.1} step={0.1} value={num("bb_mult")} onChange={(v) => patch({ bb_mult: v } as Partial<StrategyConfig>)} />
            <NumField label="KC 배수(kc_mult)" min={0.1} step={0.1} value={num("kc_mult")} onChange={(v) => patch({ kc_mult: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "trix" && (
          <>
            <NumField label="삼중 EMA 기간" min={1} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="시그널 기간" min={1} value={num("signal_period")} onChange={(v) => patch({ signal_period: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "obv_trend" && (
          <NumField label="OBV 이동평균 기간" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
        )}
        {config.type === "atr_trailing" && (
          <>
            <NumField label="채널 기간(period)" min={2} value={num("period")} onChange={(v) => patch({ period: v } as Partial<StrategyConfig>)} />
            <NumField label="ATR 기간" min={2} value={num("atr_period")} onChange={(v) => patch({ atr_period: v } as Partial<StrategyConfig>)} />
            <NumField label="ATR 배수(k)" min={0.1} step={0.1} value={num("k")} onChange={(v) => patch({ k: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "volatility_breakout" && (
          <NumField label="돌파 계수(k)" min={0.1} step={0.1} value={num("k")} onChange={(v) => patch({ k: v } as Partial<StrategyConfig>)} />
        )}
        {config.type === "keltner" && (
          <>
            <NumField label="중심선 EMA 기간" min={2} value={num("ema_period")} onChange={(v) => patch({ ema_period: v } as Partial<StrategyConfig>)} />
            <NumField label="ATR 기간" min={2} value={num("atr_period")} onChange={(v) => patch({ atr_period: v } as Partial<StrategyConfig>)} />
            <NumField label="ATR 배수(mult)" min={0.1} step={0.1} value={num("mult")} onChange={(v) => patch({ mult: v } as Partial<StrategyConfig>)} />
          </>
        )}
        {config.type === "stochastic" && (
          <>
            <NumField label="%K 기간" min={2} value={num("k_period")} onChange={(v) => patch({ k_period: v } as Partial<StrategyConfig>)} />
            <NumField label="%D 기간" min={1} value={num("d_period")} onChange={(v) => patch({ d_period: v } as Partial<StrategyConfig>)} />
            <NumField label="과매도(lower)" min={1} max={99} value={num("lower")} onChange={(v) => patch({ lower: v } as Partial<StrategyConfig>)} />
            <NumField label="과매수(upper)" min={1} max={99} value={num("upper")} onChange={(v) => patch({ upper: v } as Partial<StrategyConfig>)} />
          </>
        )}
      </div>
      )}

      {config.type === "custom" && (
        <RuleBuilder
          entry={config.entry}
          exit={config.exit}
          onChange={(field, group) =>
            patch({ [field]: group } as Partial<StrategyConfig>)
          }
        />
      )}

      {(OHLC_STRATEGY_TYPES.has(config.type) || customUsesOhlc(config)) && (
        <p className="text-xs text-amber-400">
          ※ 이 전략은 종가 외 OHLC(시·고·저)/거래량 데이터를 사용합니다. 실시간 매매 시
          일중 고·저·거래량은 폴링 시점 값으로 근사되며, 신호는 종가 확정 기준으로 평가됩니다.
        </p>
      )}

      <fieldset className="rounded-md border border-border p-3">
        <legend className="px-1 text-xs text-muted-foreground">거래비용</legend>
        <div className="grid grid-cols-2 gap-3">
          <CostField
            label="위탁수수료 % (매수·매도)"
            value={costValue("fees")}
            onChange={(v) => setCost("fees", v)}
          />
          <CostField
            label="증권거래세 % (매도 시)"
            value={costValue("tax")}
            onChange={(v) => setCost("tax", v)}
          />
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
          위탁수수료는 <b className="text-muted-foreground">매수·매도 양방향</b>, 증권거래세는{" "}
          <b className="text-muted-foreground">매도 시에만</b> 부과됩니다. 일반적으로 온라인
          위탁수수료는 <b className="text-muted-foreground">0.01~0.015%</b>, 2026년 증권거래세는
          코스피·코스닥 모두 <b className="text-muted-foreground">0.20%</b>입니다(1회 왕복 약
          0.23%). 회전율이 높은 전략일수록 비용 영향이 커지므로 실제 값을 반영해야 백테스트가
          과대평가되지 않습니다.
        </p>
      </fieldset>

      <fieldset className="rounded-md border border-border p-3">
        <legend className="px-1 text-xs text-muted-foreground">체결·현실성</legend>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="체결 시점">
            <select
              value={(config as { fill_mode?: string }).fill_mode ?? "next_close"}
              onChange={(e) =>
                patch({ fill_mode: e.target.value } as Partial<StrategyConfig>)
              }
              className={INPUT}
            >
              <option value="next_close">익일 종가(권장)</option>
              <option value="same_close">당일 종가</option>
            </select>
          </Field>
          <NumField
            label="슬리피지 (bps · 편도)"
            value={num("slippage_bps")}
            onChange={(v) => patch({ slippage_bps: v } as Partial<StrategyConfig>)}
            min={0}
            max={200}
            step={0.5}
          />
          <NumField
            label="변동성 스케일 (0=고정)"
            value={num("slippage_vol_scale")}
            onChange={(v) =>
              patch({ slippage_vol_scale: v } as Partial<StrategyConfig>)
            }
            min={0}
            max={5}
            step={0.1}
          />
          <NumField
            label="무위험수익률 % (연)"
            value={Number(((num("risk_free_rate") || 0) * 100).toFixed(4))}
            onChange={(v) =>
              patch({
                risk_free_rate: Number.isFinite(v) ? v / 100 : 0,
              } as Partial<StrategyConfig>)
            }
            min={0}
            max={20}
            step={0.1}
          />
          {config.type === "rebalance" && (
            <Field label="벤치마크 지수">
              <select
                value={(config as RebalanceConfig).benchmark_index ?? "KOSPI200"}
                onChange={(e) =>
                  patch({
                    benchmark_index: e.target.value,
                  } as Partial<StrategyConfig>)
                }
                className={INPUT}
              >
                <option value="KOSPI200">KOSPI200</option>
                <option value="KOSPI">KOSPI</option>
                <option value="KOSDAQ">KOSDAQ</option>
              </select>
            </Field>
          )}
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
          <b className="text-muted-foreground">익일 종가 체결</b>은 신호 다음 거래일 종가로
          체결해 당일 미래참조(look-ahead)를 제거합니다(권장). <b className="text-muted-foreground">
          슬리피지</b>는 편도 체결 미끄러짐(5bp=0.05%)이며, 변동성 스케일&gt;0이면 종목 변동성에
          비례해 조정합니다. <b className="text-muted-foreground">무위험수익률</b>은 샤프·소르티노의
          기준 수익률(연율)입니다.
          {config.type === "rebalance" &&
            " 벤치마크는 알파·베타·정보비율(IR) 산출 기준 지수입니다."}
        </p>
      </fieldset>

      {config.type !== "rebalance" && (
      <fieldset className="rounded-md border border-border p-3">
        <legend className="px-1 text-xs text-muted-foreground">리스크 청산 (선택 · 빈칸이면 비활성)</legend>
        <div className="grid grid-cols-3 gap-3">
          <PctField label="손절 %" value={pctValue("stop_loss_pct")} onChange={(v) => setPct("stop_loss_pct", v)} />
          <PctField label="익절 %" value={pctValue("take_profit_pct")} onChange={(v) => setPct("take_profit_pct", v)} />
          <PctField label="트레일링 %" value={pctValue("trailing_stop_pct")} onChange={(v) => setPct("trailing_stop_pct", v)} />
        </div>
      </fieldset>
      )}

      {(formError || error) && (
        <p className="text-sm text-destructive">{formError ?? error}</p>
      )}

      <div className="flex gap-2">
        <Button type="submit" disabled={pending}>
          {pending ? "처리 중…" : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel}>
            취소
          </Button>
        )}
      </div>

      <style jsx>{`
        .input {
          width: 100%;
          border-radius: 0.375rem;
          border: 1px solid #404040;
          background: #0a0a0a;
          padding: 0.5rem 0.75rem;
          font-size: 0.875rem;
          outline: none;
        }
      `}</style>
    </form>
  );
}

/** 라벨이 달린 폼 필드 래퍼. */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

/** 숫자 입력 필드(라벨 포함). */
function NumField({
  label,
  value,
  onChange,
  min,
  max,
  step,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(Number(e.target.value))}
        className={INPUT}
      />
    </Field>
  );
}

/** 퍼센트 입력 필드(빈 값 허용 = 비활성). */
function PctField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={0}
        max={100}
        step={0.1}
        placeholder="—"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT}
      />
    </Field>
  );
}

/** 거래비용(수수료·세금) 퍼센트 입력 필드. 빈 값 = 0%. */
function CostField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={0}
        max={1}
        step={0.001}
        placeholder="0"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT}
      />
    </Field>
  );
}

/** 리밸런싱 custom 선정의 기본 진입/청산 규칙(SMA5>SMA20 / SMA5<SMA20). 종가 기반만 사용. */
const DEFAULT_REBAL_ENTRY: ConditionGroup = {
  combinator: "and",
  children: [{ left: { kind: "sma", period: 5 }, op: ">", right: { kind: "sma", period: 20 } }],
};
const DEFAULT_REBAL_EXIT: ConditionGroup = {
  combinator: "and",
  children: [{ left: { kind: "sma", period: 5 }, op: "<", right: { kind: "sma", period: 20 } }],
};

/** method="score" 전환 시 기본 팩터 가중치(metrics 화면과 동일한 0.4/0.3/0.3, quality=growth=0). */
const DEFAULT_FACTOR_WEIGHTS = { momentum: 0.4, value: 0.3, lowvol: 0.3, quality: 0.0, growth: 0.0 };

/** 팩터 카테고리 메타(표시명·설명). */
const FACTOR_META: { key: keyof typeof DEFAULT_FACTOR_WEIGHTS; label: string; hint: string }[] = [
  { key: "momentum", label: "모멘텀", hint: "최근 1·3·6M 수익률·52주 고가 근접" },
  { key: "value", label: "밸류", hint: "저PER·저PBR·고배당" },
  { key: "lowvol", label: "저변동", hint: "낮은 변동성·얕은 낙폭" },
  { key: "quality", label: "퀄리티", hint: "고ROE·저부채·FCF흑자 (OpenDART 재무데이터)" },
  { key: "growth", label: "성장", hint: "영업이익·순이익 YoY 성장·흑자전환 (OpenDART 재무데이터)" },
];

/** 팩터 가중치 합계 배지. 합=1.00 이면 정상(초록), 아니면 경고(주황). */
function FactorWeightSum({ weights }: { weights: { momentum: number; value: number; lowvol: number; quality: number; growth: number } }) {
  const sum = weights.momentum + weights.value + weights.lowvol + weights.quality + weights.growth;
  const ok = Math.abs(sum - 1) < 1e-6;
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[11px] tabular-nums ${
        ok ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" : "bg-amber-500/15 text-amber-600 dark:text-amber-400"
      }`}
      title={ok ? "합계 정상" : "합계가 1.00 이어야 저장됩니다"}
    >
      합계 {sum.toFixed(2)}
    </span>
  );
}

/** 리밸런싱 전략 전용 입력 — universe·선정규칙·주기·드리프트밴드·자본. */
function RebalanceFields({
  config,
  patch,
}: {
  config: RebalanceConfig;
  patch: (p: Partial<StrategyConfig>) => void;
}) {
  const [addCode, setAddCode] = useState("");

  function addSymbol(code: string) {
    const c = code.trim();
    if (!c || config.universe.includes(c)) return;
    patch({ universe: [...config.universe, c] } as Partial<StrategyConfig>);
    setAddCode("");
  }
  function removeSymbol(code: string) {
    patch({
      universe: config.universe.filter((s) => s !== code),
    } as Partial<StrategyConfig>);
  }
  function patchSelection(p: Partial<RebalanceConfig["selection"]>) {
    patch({ selection: { ...config.selection, ...p } } as Partial<StrategyConfig>);
  }
  // 후보풀 소스(fixed=고정 목록 / 지수명=시점별 PIT 구성종목으로 생존편향 제거).
  const uniRule: UniverseRule = config.selection.universe_rule ?? { source: "fixed" };
  const pitSource = uniRule.source !== "fixed";
  const narrowing = pitSource && uniRule.type === "momentum";
  function changeUniverseSource(source: UniverseRule["source"]) {
    if (source === "fixed") {
      const { universe_rule: _drop, ...rest } = config.selection;
      patch({ selection: rest } as Partial<StrategyConfig>);
    } else {
      patchSelection({
        universe_rule: {
          source,
          type: uniRule.type ?? "momentum",
          lookback: uniRule.lookback ?? 250,
          pick: uniRule.pick ?? 40,
        },
      });
    }
  }
  function patchUniverseRule(p: Partial<UniverseRule>) {
    patchSelection({ universe_rule: { ...uniRule, ...p } });
  }
  /** 선정 방식 변경. custom 전환 시 진입/청산 규칙, score 전환 시 팩터 가중치 기본값을 채운다. */
  function changeSelectionMethod(method: RebalanceConfig["selection"]["method"]) {
    if (method === "custom") {
      // score→custom 전환 시 weighting=score 였다면 equal 로 되돌린다(백엔드 제약).
      patch({
        selection: {
          ...config.selection,
          method,
          entry: config.selection.entry ?? DEFAULT_REBAL_ENTRY,
          exit: config.selection.exit ?? DEFAULT_REBAL_EXIT,
        },
        ...(config.weighting === "score" ? { weighting: "equal" } : {}),
      } as Partial<StrategyConfig>);
    } else if (method === "score") {
      patchSelection({
        method,
        factor_weights: config.selection.factor_weights ?? { ...DEFAULT_FACTOR_WEIGHTS },
      });
    } else {
      // score→다른 방식 전환 시 weighting=score 였다면 equal 로 되돌린다(백엔드 제약).
      patch({
        selection: { ...config.selection, method },
        ...(config.weighting === "score" ? { weighting: "equal" } : {}),
      } as Partial<StrategyConfig>);
    }
  }
  /** 팩터 가중치 1개 변경(0~1). 합계는 UI 에 표시하고, 저장 시 백엔드가 합=1 을 검증한다. */
  function patchFactorWeight(key: keyof typeof DEFAULT_FACTOR_WEIGHTS, v: number) {
    const cur = config.selection.factor_weights ?? DEFAULT_FACTOR_WEIGHTS;
    patchSelection({ factor_weights: { ...cur, [key]: Number.isFinite(v) ? v : 0 } });
  }
  const regime = config.regime_filter ?? null;
  function toggleRegime(on: boolean) {
    patch({
      regime_filter: on
        ? { enabled: true, index: "KOSPI", ma_period: 200, reentry_buffer_pct: 0.05, exit_buffer_pct: 0 }
        : null,
    } as Partial<StrategyConfig>);
  }
  function patchRegime(p: Partial<NonNullable<RebalanceConfig["regime_filter"]>>) {
    if (!regime) return;
    patch({ regime_filter: { ...regime, ...p } } as Partial<StrategyConfig>);
  }
  // 리스크 레이어(P1-2): 집중 한도·변동성 타겟팅·MDD 킬스위치. null=미적용.
  const risk = config.risk_layer ?? null;
  function toggleRisk(on: boolean) {
    patch({
      risk_layer: on ? { vol_lookback: 20, max_leverage: 1.0, mdd_rearm_days: 20 } : null,
    } as Partial<StrategyConfig>);
  }
  function patchRisk(p: Partial<NonNullable<RebalanceConfig["risk_layer"]>>) {
    if (!risk) return;
    patch({ risk_layer: { ...risk, ...p } } as Partial<StrategyConfig>);
  }
  /** 리스크 레이어의 optional 비율(0~1) 필드를 퍼센트 문자열로. 미설정이면 "". */
  function riskPct(key: "max_position_pct" | "max_sector_pct" | "target_vol" | "mdd_kill_pct"): string {
    const v = risk?.[key];
    return v === null || v === undefined ? "" : String(Number((v * 100).toFixed(4)));
  }
  /** 퍼센트 입력 → 비율(0~1) 저장. 빈 값이면 null(해당 통제 비활성). */
  function setRiskPct(key: "max_position_pct" | "max_sector_pct" | "target_vol" | "mdd_kill_pct", raw: string) {
    if (raw.trim() === "") {
      patchRisk({ [key]: null } as Partial<NonNullable<RebalanceConfig["risk_layer"]>>);
      return;
    }
    const n = Number(raw);
    patchRisk({ [key]: Number.isFinite(n) ? n / 100 : null } as Partial<NonNullable<RebalanceConfig["risk_layer"]>>);
  }

  return (
    <div className="space-y-3">
      <Field label="후보풀 소스">
        <select
          value={uniRule.source}
          onChange={(e) => changeUniverseSource(e.target.value as UniverseRule["source"])}
          className={INPUT}
        >
          <option value="fixed">고정 목록(아래에서 직접 선택)</option>
          <option value="KOSPI200">KOSPI200 구성종목(시점별·PIT)</option>
          <option value="KOSPI100">KOSPI100 구성종목(시점별·PIT)</option>
          <option value="KRX300">KRX300 구성종목(시점별·PIT)</option>
        </select>
        {pitSource && (
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
            각 리밸런싱 시점의 <b>실제 지수 구성종목</b>을 후보풀로 사용합니다(편출·상폐 반영 →
            생존편향 제거). 아래 종목 목록은 지수 조회 실패 시의 <b>폴백</b>으로만 쓰입니다.
          </p>
        )}
      </Field>

      {pitSource && (
        <div className="rounded-md border p-3 space-y-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={narrowing}
              onChange={(e) =>
                patchUniverseRule(
                  e.target.checked
                    ? { type: "momentum", lookback: uniRule.lookback ?? 250, pick: uniRule.pick ?? 40 }
                    : { type: undefined },
                )
              }
            />
            <span>상대강도 상위만 후보로 축소</span>
          </label>
          {narrowing && (
            <div className="grid grid-cols-2 gap-3">
              <NumField
                label="상대강도 룩백(봉)"
                min={2}
                value={uniRule.lookback ?? 250}
                onChange={(v) => patchUniverseRule({ lookback: v })}
              />
              <NumField
                label="후보 종목 수(pick)"
                min={1}
                value={uniRule.pick ?? 40}
                onChange={(v) => patchUniverseRule({ pick: v })}
              />
            </div>
          )}
          <NumField
            label="최소 시가총액(억 원, 0=제한 없음)"
            min={0}
            step={1000}
            value={uniRule.min_market_cap ?? 0}
            onChange={(v) => patchUniverseRule({ min_market_cap: v > 0 ? v : null })}
          />
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            소형주 슬리피지 과대평가를 막는 유동성 필터. 각 리밸런싱 시점 시총 기준으로
            이 값 미만 종목을 후보풀에서 제외합니다(예: 5000 = 5000억 원).
          </p>
        </div>
      )}

      <Field
        label={
          pitSource
            ? `폴백 종목 (선택 · ${config.universe.length}개)`
            : `운용 종목 (universe · ${config.universe.length}개)`
        }
      >
        <div className="flex flex-wrap gap-2">
          {config.universe.map((code) => (
            <span
              key={code}
              className="inline-flex items-center gap-1 rounded-md border bg-secondary px-2 py-1 text-xs"
            >
              {code}
              <button
                type="button"
                onClick={() => removeSymbol(code)}
                className="text-muted-foreground hover:text-destructive"
                aria-label={`${code} 제거`}
              >
                ×
              </button>
            </span>
          ))}
          {config.universe.length === 0 && (
            <span className="text-xs text-muted-foreground">
              {pitSource ? "지수 구성종목을 자동 사용합니다(폴백 없음)." : "종목을 추가하세요."}
            </span>
          )}
        </div>
      </Field>
      <Field label="종목 추가(검색 후 선택)">
        <SymbolSearch value={addCode} onChange={addSymbol} />
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="선정 방식">
          <select
            value={config.selection.method}
            onChange={(e) => changeSelectionMethod(e.target.value as RebalanceConfig["selection"]["method"])}
            className={INPUT}
          >
            <option value="momentum">모멘텀 상위 N</option>
            <option value="score">멀티팩터 종합점수 상위 N</option>
            <option value="all">전체 동일비중</option>
            <option value="custom">사용자 규칙(편입/청산)</option>
          </select>
        </Field>
        {config.selection.method === "momentum" && (
          <>
            <NumField
              label="모멘텀 룩백(봉)"
              min={2}
              value={config.selection.lookback}
              onChange={(v) => patchSelection({ lookback: v })}
            />
            <NumField
              label="선정 종목 수(top_n)"
              min={1}
              value={config.selection.top_n}
              onChange={(v) => patchSelection({ top_n: v })}
            />
          </>
        )}
        {config.selection.method === "score" && (
          <>
            <NumField
              label="선정 종목 수(top_n)"
              min={1}
              value={config.selection.top_n}
              onChange={(v) => patchSelection({ top_n: v })}
            />
            <Field label="비중 방식">
              <select
                value={config.weighting}
                onChange={(e) =>
                  patch({ weighting: e.target.value as RebalanceConfig["weighting"] } as Partial<StrategyConfig>)
                }
                className={INPUT}
              >
                <option value="equal">동일비중</option>
                <option value="score">점수 순위 가중</option>
              </select>
            </Field>
          </>
        )}
        <NumField
          label="배정 자본(원)"
          min={1}
          value={config.capital}
          onChange={(v) => patch({ capital: v } as Partial<StrategyConfig>)}
        />
        <NumField
          label="드리프트 밴드 %"
          min={0}
          step={0.1}
          value={Number((config.drift_band_pct * 100).toFixed(4))}
          onChange={(v) =>
            patch({
              drift_band_pct: Number.isFinite(v) ? v / 100 : 0,
            } as Partial<StrategyConfig>)
          }
        />
      </div>

      {config.selection.method === "score" && (
        <div className="rounded-md border p-3 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">팩터 가중치</span>
            <FactorWeightSum weights={config.selection.factor_weights ?? DEFAULT_FACTOR_WEIGHTS} />
          </div>
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            각 카테고리 z-스코어를 가중 합산해 종합점수를 매기고 상위 {config.selection.top_n}종목을
            선정합니다. 가중치 합은 <b className="text-muted-foreground">1.00</b>이어야 저장됩니다.
            퀄리티는 OpenDART 재무데이터가 필요하며, 키가 없으면 중립 처리됩니다.
          </p>
          <Field label="팩터 중립화">
            <select
              value={config.selection.neutralize ?? "none"}
              onChange={(e) =>
                patchSelection({
                  neutralize: e.target.value as "none" | "size",
                })
              }
              className={INPUT}
            >
              <option value="none">없음</option>
              <option value="size">시가총액 중립화</option>
            </select>
          </Field>
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            <b className="text-muted-foreground">시가총액 중립화</b>는 각 팩터 점수를 로그
            시가총액 축에 직교화해, 팩터가 의도치 않게 대형/소형주 베팅으로 변질되는 것을
            막습니다(순수 팩터 노출). 시가총액(PIT)이 필요하며, 조회 실패 시 중립화는 생략됩니다.
          </p>
          <div className="space-y-2.5">
            {FACTOR_META.map((f) => {
              const w = (config.selection.factor_weights ?? DEFAULT_FACTOR_WEIGHTS)[f.key];
              return (
                <div key={f.key} className="grid grid-cols-[5rem_1fr_3rem] items-center gap-2">
                  <span className="text-xs font-medium" title={f.hint}>
                    {f.label}
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={w}
                    onChange={(e) => patchFactorWeight(f.key, Number(e.target.value))}
                    className="w-full accent-primary"
                    aria-label={`${f.label} 가중치`}
                  />
                  <span className="text-right text-xs tabular-nums text-muted-foreground">
                    {w.toFixed(2)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {config.selection.method === "custom" && (
        <div className="space-y-2">
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            universe 각 종목에 아래 규칙을 독립 적용해, 진입 신호가 난 뒤 아직 청산되지
            않은(현재 보유 국면) 종목만 동일비중으로 편입합니다. top_n·룩백은 사용하지 않습니다.
            리밸런싱은 종가만 사용하므로 <b className="text-muted-foreground">종가 기반 지표
            (가격 종가·SMA·EMA·RSI·MACD·상수)</b>만 쓸 수 있습니다(OHLC/거래량 불가).
          </p>
          <RuleBuilder
            entry={config.selection.entry ?? DEFAULT_REBAL_ENTRY}
            exit={config.selection.exit ?? DEFAULT_REBAL_EXIT}
            onChange={(field, group) =>
              patchSelection({ [field]: group } as Partial<RebalanceConfig["selection"]>)
            }
          />
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <Field label="리밸런싱 주기">
          <select
            value={config.cadence}
            onChange={(e) =>
              patch({
                cadence: e.target.value as RebalanceConfig["cadence"],
              } as Partial<StrategyConfig>)
            }
            className={INPUT}
          >
            <option value="daily">매일</option>
            <option value="weekly">매주</option>
            <option value="monthly">매월</option>
            <option value="quarterly">매분기</option>
          </select>
        </Field>
        <Field label="실행 시각 (HH:MM, KST)">
          <input
            type="time"
            value={config.rebalance_time}
            onChange={(e) =>
              patch({ rebalance_time: e.target.value } as Partial<StrategyConfig>)
            }
            className={INPUT}
          />
        </Field>
        {config.cadence === "weekly" && (
          <Field label="실행 요일">
            <select
              value={config.rebalance_weekday ?? 0}
              onChange={(e) =>
                patch({
                  rebalance_weekday: Number(e.target.value),
                } as Partial<StrategyConfig>)
              }
              className={INPUT}
            >
              {["월", "화", "수", "목", "금"].map((d, i) => (
                <option key={i} value={i}>
                  {d}요일
                </option>
              ))}
            </select>
          </Field>
        )}
        {(config.cadence === "monthly" || config.cadence === "quarterly") && (
          <NumField
            label={config.cadence === "quarterly" ? "실행 일자(분기 첫달 1~28)" : "실행 일자(1~28)"}
            min={1}
            max={28}
            value={config.rebalance_dom ?? 1}
            onChange={(v) =>
              patch({ rebalance_dom: v } as Partial<StrategyConfig>)
            }
          />
        )}
      </div>

      <div className="rounded-md border p-3 space-y-2">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={!!config.initial_fill_immediate}
            onChange={(e) =>
              patch({
                initial_fill_immediate: e.target.checked,
              } as Partial<StrategyConfig>)
            }
          />
          최초 실행 시 즉시 매수(콜드 스타트)
        </label>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          전략을 처음 실시간 실행할 때 다음 리밸런싱 발화일·시각을 기다리지 않고, 장중이면
          즉시 1회 초기 매수합니다. 첫 실행(보유 없음)에만 적용되며 이후 주기는 위 설정을
          따릅니다. 레짐 위험회피 국면이면 매수하지 않고 현금을 유지합니다.
        </p>
      </div>

      <div className="rounded-md border p-3 space-y-3">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={!!regime?.enabled}
            onChange={(e) => toggleRegime(e.target.checked)}
          />
          현금화 오버레이(레짐 필터)
        </label>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          기준지수가 이동평균 아래(하락 추세)면 보유를 전량 청산·현금화하고 신규 매수를
          중단합니다. 추세가 이동평균을 재진입 버퍼만큼 회복하면 그 즉시 재편입합니다(하락장 방어).
        </p>
        {regime?.enabled && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Field label="기준지수">
                <select
                  value={regime.index}
                  onChange={(e) =>
                    patchRegime({ index: e.target.value as "KOSPI" | "KOSDAQ" })
                  }
                  className={INPUT}
                >
                  <option value="KOSPI">KOSPI</option>
                  <option value="KOSDAQ">KOSDAQ</option>
                </select>
              </Field>
              <NumField
                label="이동평균 기간(거래일)"
                min={5}
                max={400}
                value={regime.ma_period}
                onChange={(v) => patchRegime({ ma_period: v })}
              />
              <NumField
                label="재진입 버퍼 %"
                min={0}
                max={20}
                step={0.5}
                value={Number(((regime.reentry_buffer_pct ?? 0) * 100).toFixed(4))}
                onChange={(v) =>
                  patchRegime({ reentry_buffer_pct: Number.isFinite(v) ? v / 100 : 0 })
                }
              />
              <NumField
                label="청산 버퍼 %"
                min={0}
                max={20}
                step={0.5}
                value={Number(((regime.exit_buffer_pct ?? 0) * 100).toFixed(4))}
                onChange={(v) =>
                  patchRegime({ exit_buffer_pct: Number.isFinite(v) ? v / 100 : 0 })
                }
              />
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              재진입 버퍼: 지수가 이동평균을 이 비율만큼 상회해야 재편입(가짜 반등·휩쏘 억제,
              권장 5%). 청산 버퍼: 이동평균을 이 비율만큼 하회할 때 청산(0%면 하회 즉시).
            </p>
          </>
        )}
      </div>

      <div className="rounded-md border p-3 space-y-3">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={!!risk}
            onChange={(e) => toggleRisk(e.target.checked)}
          />
          포트폴리오 리스크 레이어
        </label>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          선정·비중 산정 이후 목표비중에 위험 통제를 적용합니다. 각 항목은 빈칸이면 비활성입니다.
          집중 한도는 소수 종목 쏠림을, 변동성 타겟팅은 고변동 국면 노출을, MDD 킬스위치는
          파국적 낙폭을 제한합니다.
        </p>
        {risk && (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <PctField
                label="종목 집중 한도 %"
                value={riskPct("max_position_pct")}
                onChange={(v) => setRiskPct("max_position_pct", v)}
              />
              <PctField
                label="섹터 집중 한도 %"
                value={riskPct("max_sector_pct")}
                onChange={(v) => setRiskPct("max_sector_pct", v)}
              />
              <PctField
                label="목표 변동성 % (연)"
                value={riskPct("target_vol")}
                onChange={(v) => setRiskPct("target_vol", v)}
              />
              <PctField
                label="MDD 킬스위치 %"
                value={riskPct("mdd_kill_pct")}
                onChange={(v) => setRiskPct("mdd_kill_pct", v)}
              />
              {risk.target_vol != null && (
                <>
                  <NumField
                    label="변동성 룩백(거래일)"
                    min={5}
                    max={120}
                    value={risk.vol_lookback ?? 20}
                    onChange={(v) => patchRisk({ vol_lookback: v })}
                  />
                  <NumField
                    label="최대 투자비중(≤1)"
                    min={0.1}
                    max={1}
                    step={0.05}
                    value={risk.max_leverage ?? 1.0}
                    onChange={(v) => patchRisk({ max_leverage: v })}
                  />
                </>
              )}
              {risk.mdd_kill_pct != null && (
                <NumField
                  label="킬 쿨다운(거래일)"
                  min={1}
                  max={250}
                  value={risk.mdd_rearm_days ?? 20}
                  onChange={(v) => patchRisk({ mdd_rearm_days: v })}
                />
              )}
            </div>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              <b className="text-muted-foreground">종목 집중 한도</b>는 드리프트 밴드(
              {Number((config.drift_band_pct * 100).toFixed(2))}%)보다 커야 진입이 발생합니다.
              <b className="text-muted-foreground"> 섹터 집중 한도</b>는 업종 합산 비중을 제한하며
              (KRX 업종 매핑 조회 실패 시 조용히 미적용),
              <b className="text-muted-foreground"> 변동성 타겟팅</b>은 실현변동성이 목표를 넘으면
              투자비중을 줄이며(차입 없음, 확대는 안 함), <b className="text-muted-foreground">킬스위치</b>는
              고점 대비 낙폭이 임계를 넘으면 전량 청산 후 쿨다운 뒤 재가동합니다.
            </p>
          </>
        )}
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        ※ universe 종목은 이 전략이 단독으로 운용한다고 가정합니다(다른 전략과 종목이
        겹치지 않게 하세요). 지정 시각 이후 가장 가까운 영업일·장중에 실행됩니다.
      </p>
    </div>
  );
}

/** custom 전략이 종가 외 OHLC/거래량 source 를 참조하는지. */
function customUsesOhlc(config: StrategyConfig): boolean {
  if (config.type !== "custom") return false;
  const conds = [
    ...flattenConditions(config.entry),
    ...flattenConditions(config.exit),
  ];
  return conds.some((c) =>
    [c.left, c.right].some(
      (o) =>
        o.kind === "price" &&
        o.source != null &&
        o.source !== "close",
    ),
  );
}

/** custom 피연산자의 필수 필드 검증. @returns 오류 메시지 또는 null */
function validateOperand(o: Operand): string | null {
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

/**
 * 전략 알고리즘 설명·수식 카드(유형 선택 시 표시).
 * custom 은 현재 규칙으로부터 수식을 동적 생성한다.
 */
function AlgorithmInfo({ config }: { config: StrategyConfig }) {
  if (config.type === "custom") {
    const { entry, exit } = formatCustomFormula(config);
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
        <p className="text-foreground">사용자가 진입·청산 규칙을 직접 조합하는 전략입니다.</p>
        <div className="mt-2 space-y-1 font-mono text-[12px] text-muted-foreground">
          <p>진입: {entry}</p>
          <p>청산: {exit}</p>
        </div>
      </div>
    );
  }

  if (config.type === "rebalance") {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
        <p className="text-foreground">
          여러 종목(universe)을 운용하며 주기적으로 목표 비중을 재산정해, 목표 대비
          편차가 드리프트 밴드를 넘는 종목만 매매(리밸런싱)합니다.
        </p>
        <div className="mt-2 space-y-1 font-mono text-[12px] text-muted-foreground">
          <p>선정: 모멘텀(룩백 수익률) 상위 N 종목 또는 전체</p>
          <p>비중: 선정 종목 동일비중(1/N)</p>
          <p>매매: |현재비중 − 목표비중| &gt; 드리프트 밴드 인 종목만</p>
        </div>
      </div>
    );
  }

  const info = STRATEGY_INFO[config.type];
  return (
    <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
      <p className="text-foreground">{info.description}</p>
      <div className="mt-2 space-y-1 font-mono text-[12px] text-muted-foreground">
        {info.formula.map((line, i) => (
          <p key={i}>{line}</p>
        ))}
      </div>
    </div>
  );
}
