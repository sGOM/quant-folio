"use client";

import { useState } from "react";
import { Operand, RebalanceConfig, StrategyConfig, StrategyType } from "@/lib/api";
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

/** 입력/셀렉트 공용 스타일(shadcn input 토큰). 자식 컴포넌트에도 동일 톤을 적용한다. */
const INPUT =
  "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

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
    // 편집 시 거래비용 필드(fees/tax)가 없는 레거시 설정은 기본값으로 채운다(없는 키만 보강).
    initialConfig
      ? ({
          ...initialConfig,
          fees: initialConfig.fees ?? 0.00015,
          tax: initialConfig.tax ?? 0.002,
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
    const { symbol, cash, fees, tax, stop_loss_pct, take_profit_pct, trailing_stop_pct } =
      config;
    setConfig(
      defaultConfig(type, {
        symbol,
        cash,
        fees: fees ?? 0.00015,
        tax: tax ?? 0.002,
        stop_loss_pct,
        take_profit_pct,
        trailing_stop_pct,
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
      if (config.universe.length < 1) return "리밸런싱 종목을 1개 이상 추가하세요.";
      if (config.universe.some((s) => !s.trim())) return "빈 종목코드가 있습니다.";
      if (new Set(config.universe).size !== config.universe.length)
        return "중복 종목코드가 있습니다.";
      if (
        config.selection.method === "momentum" &&
        config.selection.top_n > config.universe.length
      )
        return "선정 종목 수(top_n)는 종목 수 이하여야 합니다.";
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

  return (
    <div className="space-y-3">
      <Field label={`운용 종목 (universe · ${config.universe.length}개)`}>
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
            <span className="text-xs text-muted-foreground">종목을 추가하세요.</span>
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
            onChange={(e) =>
              patchSelection({ method: e.target.value as "momentum" | "all" })
            }
            className={INPUT}
          >
            <option value="momentum">모멘텀 상위 N</option>
            <option value="all">전체 동일비중</option>
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
        {config.cadence === "monthly" && (
          <NumField
            label="실행 일자(1~28)"
            min={1}
            max={28}
            value={config.rebalance_dom ?? 1}
            onChange={(v) =>
              patch({ rebalance_dom: v } as Partial<StrategyConfig>)
            }
          />
        )}
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        ※ 리밸런싱은 백테스트를 지원하지 않습니다. universe 종목은 이 전략이 단독으로
        운용한다고 가정합니다(다른 전략과 종목이 겹치지 않게 하세요). 지정 시각 이후
        가장 가까운 영업일·장중에 실행됩니다.
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
