"use client";

import { useState } from "react";
import { ConditionGroup, RebalanceConfig, StrategyConfig, UniverseRule } from "@/lib/api";
import { RuleBuilder } from "@/components/RuleBuilder";
import { SymbolSearch } from "@/components/SymbolSearch";
import { Field, INPUT, NumField, PctField } from "./fields";
import { DEFAULT_FACTOR_WEIGHTS, FactorWeights } from "./validate";

/** 리밸런싱 custom 선정의 기본 진입/청산 규칙(SMA5>SMA20 / SMA5<SMA20). 종가 기반만 사용. */
const DEFAULT_REBAL_ENTRY: ConditionGroup = {
  combinator: "and",
  children: [{ left: { kind: "sma", period: 5 }, op: ">", right: { kind: "sma", period: 20 } }],
};
const DEFAULT_REBAL_EXIT: ConditionGroup = {
  combinator: "and",
  children: [{ left: { kind: "sma", period: 5 }, op: "<", right: { kind: "sma", period: 20 } }],
};

/** 팩터 카테고리 메타(표시명·설명). */
const FACTOR_META: { key: keyof FactorWeights; label: string; hint: string }[] = [
  { key: "momentum", label: "모멘텀", hint: "최근 1·3·6M 수익률·52주 고가 근접" },
  { key: "value", label: "밸류", hint: "저PER·저PBR·고배당" },
  { key: "lowvol", label: "저변동", hint: "낮은 변동성·얕은 낙폭" },
  { key: "quality", label: "퀄리티", hint: "고ROE·저부채·FCF흑자 (OpenDART 재무데이터)" },
  { key: "growth", label: "성장", hint: "영업이익·순이익 YoY 성장·흑자전환 (OpenDART 재무데이터)" },
];

/** 팩터 가중치 합계 배지. 합=1.00 이면 정상(초록), 아니면 경고(주황). */
function FactorWeightSum({ weights }: { weights: FactorWeights }) {
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
export function RebalanceFields({
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
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
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
  function patchFactorWeight(key: keyof FactorWeights, v: number) {
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
          <Field label="재무데이터 반영 주기(퀄리티·성장 팩터)">
            <select
              value={config.financial_period ?? "annual"}
              onChange={(e) =>
                patch({
                  financial_period: e.target.value as "annual" | "ttm",
                } as Partial<StrategyConfig>)
              }
              className={INPUT}
            >
              <option value="annual">연간(사업보고서, 기본)</option>
              <option value="ttm">분기 TTM(트레일링 4분기)</option>
            </select>
          </Field>
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            <b className="text-muted-foreground">분기 TTM</b>은 최근 4개 분기 실적을 합산해
            재무 반영 시차를 분기 단위로 좁힙니다(둘 다 미래참조 없음 — as_of 시점에 이미
            공시된 보고서만 사용). 기본은 연간(기존 등록 전략 재현성 보존).
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
