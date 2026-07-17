"use client";

import { useState } from "react";
import { RebalanceConfig, StrategyConfig, StrategyType } from "@/lib/api";
import {
  STRATEGY_TYPES,
  STRATEGY_TYPE_LABELS,
  OHLC_STRATEGY_TYPES,
  defaultConfig,
} from "@/lib/strategy";
import { RuleBuilder } from "@/components/RuleBuilder";
import { SymbolSearch } from "@/components/SymbolSearch";
import { Button } from "@/components/ui/button";
import { AlgorithmInfo } from "@/components/strategy-form/AlgorithmInfo";
import { CostField, Field, INPUT, NumField, PctField } from "@/components/strategy-form/fields";
import { IndicatorParamFields } from "@/components/strategy-form/IndicatorParamFields";
import { RebalanceFields } from "@/components/strategy-form/RebalanceFields";
import { customUsesOhlc, validateStrategyForm } from "@/components/strategy-form/validate";

/**
 * 전략 생성·편집 공용 폼. 유형 선택에 따라 파라미터 입력 필드를 동적으로 렌더하고,
 * 공통 필드(종목·초기자본·손절/익절/트레일링)를 함께 입력받는다.
 *
 * 하위 모듈(components/strategy-form/):
 * - fields         입력 프리미티브(Field·NumField·PctField·CostField·INPUT 스타일)
 * - validate       제출 전 검증(순수 함수)
 * - IndicatorParamFields  단일종목 전략의 유형별 지표 파라미터
 * - RebalanceFields       리밸런싱 전략 전용 입력(universe·선정·주기·레짐·리스크 레이어)
 * - AlgorithmInfo         전략 설명·수식 카드
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

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const err = validateStrategyForm(name, config);
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

      {config.type === "rebalance" ? (
        <RebalanceFields config={config as RebalanceConfig} patch={patch} />
      ) : (
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
          <IndicatorParamFields config={config} patch={patch} />
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
        {config.type === "rebalance" && (
          <>
            <label className="mt-2 flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={!!(config as RebalanceConfig).price_limit_model}
                onChange={(e) =>
                  patch({
                    price_limit_model: e.target.checked,
                  } as Partial<StrategyConfig>)
                }
              />
              상하한가·호가단위 반영
            </label>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              켜면 체결가를 KRX 가격대별 호가단위로 라운딩하고, 전일종가 대비 ±30%
              상하한가에 도달한 방향(매수=상한가·매도=하한가)의 주문을 그날 체결
              불가로 막아 다음 리밸런싱으로 이월합니다. 꺼두면(기본) 상하한가는
              슬리피지에 근사 흡수된 것으로 봅니다.
            </p>
          </>
        )}
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

    </form>
  );
}
