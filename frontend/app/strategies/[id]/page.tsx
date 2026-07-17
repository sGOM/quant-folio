"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  Backtest,
  DsrAnalysis,
  FactorIC,
  StrategyConfig,
  TrackingResponse,
} from "@/lib/api";
import { Nav } from "@/components/Nav";
import { LineChart, OverlayLineChart } from "@/components/LineChart";
import { RequireAuth } from "@/components/RequireAuth";
import { StrategyForm } from "@/components/StrategyForm";
import { TradeLogTable } from "@/components/TradeLogTable";
import { DsrGradeBadge } from "@/components/DsrGradeBadge";
import { summarizeConfig } from "@/lib/strategy";
import { useSymbolNames } from "@/lib/useSymbolNames";
import { fmtNum, fmtPct, pctColor } from "@/lib/format";
import { alignOverlaySeries, fmtTrackingError } from "@/lib/tracking";

// lib/format 공용 헬퍼에 이 화면의 표준 자릿수(2)만 입힌 별칭 — 포맷 로직 중복 금지.
const pct = (x: number | null | undefined) => fmtPct(x, 2);
const num = (x: number | null | undefined, digits = 2) => fmtNum(x, digits);

/** 전략 상세 라우트. 동적 params 를 풀어 인증 게이트로 감싼 콘텐츠에 전달한다. */
export default function StrategyDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <RequireAuth>
      <StrategyDetailContent sid={Number(id)} />
    </RequireAuth>
  );
}

/**
 * 전략 상세 본문. 기간을 지정해 백테스트를 실행하고, 최신 결과(성과 지표·자산 곡선)와
 * 실행 이력을 보여준다.
 * @param sid 전략 ID
 */
function StrategyDetailContent({ sid }: { sid: number }) {
  const qc = useQueryClient();
  const router = useRouter();

  const today = new Date().toISOString().slice(0, 10);
  const [start, setStart] = useState("2023-01-01");
  const [end, setEnd] = useState(today);
  const [editing, setEditing] = useState(false);

  const strategy = useQuery({
    queryKey: ["strategy", sid],
    queryFn: () => api.getStrategy(sid),
  });
  // 종목코드 → 한글명 매핑(체결 로그에 종목명 표시). 캐시 정책은 훅 참조.
  const { nameOf } = useSymbolNames();
  const backtests = useQuery({
    queryKey: ["backtests", sid],
    queryFn: () => api.listBacktests(sid),
  });

  const run = useMutation({
    mutationFn: () => api.runBacktest(sid, start, end),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backtests", sid] });
      qc.invalidateQueries({ queryKey: ["strategy", sid] });
    },
  });

  const edit = useMutation({
    mutationFn: ({
      name,
      config,
      description,
    }: {
      name: string;
      config: StrategyConfig;
      description: string;
    }) => api.updateStrategy(sid, name, config, description),
    onSuccess: () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["strategy", sid] });
    },
  });

  // 대표 백테스트 지정/해제(공유 시 성과 표시용).
  const setFeatured = useMutation({
    mutationFn: (backtestId: number | null) =>
      api.setFeaturedBacktest(sid, backtestId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategy", sid] }),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteStrategy(sid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
      router.push("/strategies");
    },
  });

  /** 삭제 전 확인 후 전략을 제거한다. */
  const handleDelete = () => {
    if (
      window.confirm(
        `'${strategy.data?.name ?? "이 전략"}'을(를) 삭제할까요?\n백테스트 이력도 함께 삭제되며 되돌릴 수 없습니다.`,
      )
    ) {
      remove.mutate();
    }
  };

  // 서버 정렬에 의존하지 않고 created_at 최신을 선택.
  const latest: Backtest | undefined = backtests.data
    ? [...backtests.data].sort(
        (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
      )[0]
    : undefined;

  const isRebalance = strategy.data?.config.type === "rebalance";

  // DSR(Deflated Sharpe Ratio, P2-1) — 최신 백테스트에 한해 온디맨드 계산(가벼운 순수함수).
  const dsr = useQuery({
    queryKey: ["backtest-dsr", latest?.id],
    queryFn: () => api.getBacktestDsr(latest!.id),
    enabled: !!latest?.id,
  });

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Link
          href="/strategies"
          className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          ← 전략 목록으로
        </Link>
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">{strategy.data?.name ?? "전략"}</h1>
          {strategy.data && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => setEditing((v) => !v)}
                className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
              >
                {editing ? "닫기" : "편집"}
              </button>
              <button
                onClick={handleDelete}
                disabled={remove.isPending}
                className="rounded-md border border-destructive/40 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/10 disabled:opacity-50"
              >
                {remove.isPending ? "삭제 중…" : "삭제"}
              </button>
            </div>
          )}
        </div>
        {remove.isError && (
          <p className="mt-2 text-sm text-destructive">
            삭제 실패: {(remove.error as Error).message}
          </p>
        )}
        {strategy.data && (
          <p className="mt-1 text-sm text-muted-foreground">
            {summarizeConfig(strategy.data.config)}
            {strategy.data.config.type === "rebalance"
              ? ` · 배정자본 ${strategy.data.config.capital.toLocaleString()}원`
              : ` · 초기자본 ${strategy.data.config.cash.toLocaleString()}원`}
          </p>
        )}
        {strategy.data?.description && (
          <p className="mt-2 whitespace-pre-wrap rounded-md border border-border bg-card/50 p-3 text-sm text-foreground/90">
            {strategy.data.description}
          </p>
        )}

        {editing && strategy.data && (
          <StrategyForm
            initialName={strategy.data.name}
            initialDescription={strategy.data.description ?? ""}
            initialConfig={strategy.data.config}
            submitLabel="변경 저장"
            pending={edit.isPending}
            error={edit.isError ? (edit.error as Error).message : null}
            onSubmit={(name, config, description) =>
              edit.mutate({ name, config, description })
            }
            onCancel={() => setEditing(false)}
          />
        )}

        <section className="mt-6 flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-4">
          <label className="space-y-1">
            <span className="block text-xs text-muted-foreground">시작일</span>
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-xs text-muted-foreground">종료일</span>
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </label>
          <button
            onClick={() => run.mutate()}
            disabled={run.isPending}
            className="rounded-md bg-primary px-4 py-2 text-sm hover:bg-primary/90 disabled:opacity-50"
          >
            {run.isPending ? "백테스트 실행 중…" : "백테스트 실행"}
          </button>
          {run.isError && (
            <span className="text-sm text-destructive">{(run.error as Error).message}</span>
          )}
        </section>

        {latest?.result && (
          <section className="mt-6 space-y-4">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Metric label="총수익률" value={pct(latest.total_return)} accent />
              <Metric label="최대낙폭(MDD)" value={pct(latest.mdd)} />
              <Metric
                label="샤프지수"
                value={latest.sharpe?.toFixed(2) ?? "-"}
              />
              {isRebalance ? (
                <Metric
                  label="CAGR / 매매수"
                  value={`${pct(latest.result.cagr)} / ${latest.result.num_trades}`}
                />
              ) : (
                <Metric
                  label="승률 / 매매수"
                  value={`${pct(latest.result.win_rate)} / ${latest.result.num_trades}`}
                />
              )}
              <Metric label="소르티노" value={num(latest.result.sortino)} />
            </div>

            {dsr.data && <DsrCard dsr={dsr.data} />}

            {isRebalance &&
              latest.result.benchmark_return !== null &&
              latest.result.benchmark_return !== undefined && (
                <div className="rounded-lg border border-border bg-card p-4">
                  <h2 className="mb-3 text-sm text-muted-foreground">
                    벤치마크 상대성과 (KOSPI200)
                  </h2>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                    <Metric
                      label="벤치마크 수익"
                      value={pct(latest.result.benchmark_return)}
                    />
                    <Metric
                      label="초과수익"
                      value={pct(latest.result.excess_return)}
                      accent
                    />
                    <Metric label="알파(연)" value={pct(latest.result.alpha)} />
                    <Metric label="베타" value={num(latest.result.beta)} />
                    <Metric
                      label="정보비율(IR)"
                      value={num(latest.result.information_ratio)}
                    />
                    <Metric
                      label="추적오차"
                      value={pct(latest.result.tracking_error)}
                    />
                  </div>
                  <p className="mt-3 text-xs text-muted-foreground">
                    알파·베타는 KOSPI200 대비 일간수익 회귀 기준. 알파&gt;0·베타&lt;1 이면
                    저위험 초과수익. 정보비율은 초과수익의 일관성(≥0.5 양호, ≥1 우수).
                    샤프·소르티노는 무위험수익률 초과 기준(전략 config의 risk_free_rate).
                  </p>
                </div>
              )}

            {isRebalance &&
              latest.result.factor_ic &&
              Object.keys(latest.result.factor_ic).length > 0 && (
                <FactorICCard factorIc={latest.result.factor_ic} />
              )}

            <div className="rounded-lg border border-border bg-card p-4">
              <h2 className="mb-2 text-sm text-muted-foreground">자산 곡선 (Equity Curve)</h2>
              <LineChart data={latest.result.equity_curve} />
              {isRebalance && (
                <p className="mt-2 text-xs text-muted-foreground">
                  리밸런싱 {latest.result.num_rebalances ?? 0}회 · 평균 회전율{" "}
                  {pct(latest.result.avg_turnover)}
                  {latest.result.num_kills != null && latest.result.num_kills > 0 && (
                    <span className="text-amber-600 dark:text-amber-400">
                      {" "}· MDD 킬스위치 {latest.result.num_kills}회 발동
                    </span>
                  )}
                </p>
              )}
            </div>

            {isRebalance &&
              latest.result.holdings &&
              Object.keys(latest.result.holdings).length > 0 && (
                <div className="rounded-lg border border-border bg-card p-4">
                  <h2 className="mb-2 text-sm text-muted-foreground">
                    종료 시점 보유 종목 (비중)
                  </h2>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(latest.result.holdings).map(([sym, w]) => (
                      <span
                        key={sym}
                        className="rounded-md border border-border bg-background px-2 py-1 text-xs"
                      >
                        {sym} · {pct(w)}
                      </span>
                    ))}
                  </div>
                </div>
              )}

            {isRebalance &&
              latest.result.trades &&
              latest.result.trades.length > 0 && (
                <TradeLogTable trades={latest.result.trades} nameOf={nameOf} />
              )}
          </section>
        )}

        <section className="mt-8">
          <h2 className="mb-2 text-sm text-muted-foreground">
            실측 vs 백테스트 (모의투자/라이브 NAV 비교)
          </h2>
          <TrackingPanel sid={sid} />
        </section>

        <section className="mt-8">
          <h2 className="mb-2 text-sm text-muted-foreground">
            백테스트 이력
            <span className="ml-2 text-xs">
              · ★ 대표로 지정하면 공유 시 성과가 함께 표시됩니다
            </span>
          </h2>
          <div className="space-y-2">
            {backtests.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">아직 실행한 백테스트가 없습니다.</p>
            )}
            {backtests.data?.map((b) => {
              const featured = strategy.data?.featured_backtest_id === b.id;
              return (
                <div
                  key={b.id}
                  className={`flex flex-wrap items-center justify-between gap-2 rounded-md border px-4 py-2 text-sm ${
                    featured
                      ? "border-primary/50 bg-primary/5"
                      : "border-border bg-card"
                  }`}
                >
                  <span className="text-muted-foreground">
                    {b.period_start.slice(0, 10)} ~ {b.period_end.slice(0, 10)}
                  </span>
                  <div className="flex items-center gap-3">
                    <span>
                      수익률 {pct(b.total_return)} · MDD {pct(b.mdd)} · 샤프{" "}
                      {b.sharpe?.toFixed(2) ?? "-"}
                    </span>
                    <button
                      onClick={() => setFeatured.mutate(featured ? null : b.id)}
                      disabled={setFeatured.isPending}
                      className={`shrink-0 rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-50 ${
                        featured
                          ? "border-primary bg-primary/10 text-primary"
                          : "border-input text-muted-foreground hover:bg-accent"
                      }`}
                    >
                      {featured ? "★ 대표" : "대표 지정"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
          {setFeatured.isError && (
            <p className="mt-2 text-sm text-destructive">
              {(setFeatured.error as Error).message}
            </p>
          )}
        </section>
      </main>
    </>
  );
}

/**
 * 성과 지표 카드(라벨 + 값).
 * @param label  지표 이름
 * @param value  표시 값
 * @param accent true 면 값을 강조색(파랑)으로 표시
 */
function Metric({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-1 text-lg font-semibold ${accent ? "text-primary" : ""}`}>
        {value}
      </p>
    </div>
  );
}

/** 이미 %로 계산된 값을 부호 포함 문자열로. null → "-" (fmtPct 는 소수 비율 입력을 가정해 재사용 불가). */
function pctStr(v: number | null | undefined, digits = 2): string {
  return v == null ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/**
 * 전략 실측(모의투자/라이브) NAV ↔ 백테스트 기대곡선 오버레이 패널(improvements.md §6).
 * 체결이 하나도 없으면(404) "아직 실측 데이터 없음" 안내로 자연스럽게 처리하고,
 * 그 밖의 실패만 에러로 보여준다. 비교할 백테스트가 없으면 warning 문구를 노출한다.
 * @param sid 전략 ID
 */
function TrackingPanel({ sid }: { sid: number }) {
  const tracking = useQuery({
    queryKey: ["strategy-tracking", sid],
    queryFn: () => api.getStrategyTracking(sid),
    // 체결 없음(404)은 정상 상태(아직 실측 없음)라 재시도할 필요가 없다.
    retry: (failureCount, err) =>
      err instanceof ApiError && err.status === 404 ? false : failureCount < 2,
  });

  if (tracking.isLoading) {
    return <p className="text-sm text-muted-foreground">실측 데이터 조회 중…</p>;
  }
  if (tracking.isError) {
    const err = tracking.error;
    if (err instanceof ApiError && err.status === 404) {
      return (
        <p className="text-sm text-muted-foreground">
          아직 실측 데이터가 없습니다(모의투자/실거래 체결 후 이용 가능합니다).
        </p>
      );
    }
    return (
      <p className="text-sm text-destructive">
        실측 비교 리포트를 불러오지 못했습니다: {err instanceof Error ? err.message : "알 수 없는 오류"}
      </p>
    );
  }

  const data: TrackingResponse | undefined = tracking.data;
  if (!data) return null;

  // 백테스트 곡선은 보통 실측보다 훨씬 긴 기간이라, 실측 창으로 잘라 재기준한 뒤 겹친다.
  const overlay = alignOverlaySeries(data.realized_index, data.backtest_index);
  const m = data.metrics;

  return (
    <div className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric label="연율화 트래킹에러" value={fmtTrackingError(m.tracking_error)} />
        <Metric label="누적 괴리율" value={pctStr(m.cumulative_divergence_pct)} />
        <Metric label="실측 수익률(공통구간)" value={pctStr(m.realized_return_pct)} />
        <Metric label="백테스트 수익률(공통구간)" value={pctStr(m.backtest_return_pct)} />
      </div>
      <p className="text-xs text-muted-foreground">
        공통 거래일 {m.n_common_days}일
        {m.correlation != null && ` · 일수익률 상관 ${m.correlation.toFixed(2)}`}
        {" · "}체결 {data.sample.n_executions}건 / 종목 {data.sample.n_symbols}개 / 실측{" "}
        {data.sample.n_realized_days}거래일
        {data.window && ` · 창 ${data.window.date_from} ~ ${data.window.date_to}`}
      </p>

      <OverlayLineChart
        series={[
          { name: "실측 NAV(정규화)", color: "#3b82f6", data: overlay.realized },
          { name: "백테스트 기대(정규화)", color: "#f59e0b", data: overlay.backtest },
        ]}
      />

      {data.warning && (
        <p className="rounded-md border border-dashed border-amber-500/40 bg-amber-500/5 p-2 text-xs text-amber-700 dark:text-amber-400">
          {data.warning}
        </p>
      )}
      {data.notes.length > 0 && (
        <ul className="list-inside list-disc text-xs text-muted-foreground">
          {data.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted-foreground">
        두 곡선 모두 100 기준으로 정규화했고, 백테스트 곡선은 실측 구간에 맞춰 잘라 다시
        100으로 재기준했다(같은 출발선에서 벌어진 정도를 비교하기 위함). 트래킹에러는 공통
        거래일 일수익률 차이의 연율화 표준편차, 누적 괴리율은 공통구간 첫날 대비 마지막날
        상대 성과 격차다(양수=실측이 백테스트를 초과).
      </p>
    </div>
  );
}

/**
 * DSR(Deflated Sharpe Ratio, P2-1) 카드 — 과최적화(다중검정 selection bias) 방어 지표.
 * 같은 전략·같은 기간의 백테스트 이력(파라미터 탐색 시행들)이 쌓일수록 추정이
 * 정교해진다(N/N_eff). dsr<0.95 면 등급 배지로 경고해 "보일 때만 행동을 바꾸는" 방어
 * 통계의 취지를 살린다.
 */
function DsrCard({ dsr }: { dsr: DsrAnalysis }) {
  const hasDsr = dsr.status === "ok" && dsr.dsr !== null && dsr.dsr !== undefined;

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-sm text-muted-foreground">
          Deflated Sharpe Ratio(DSR)
        </h2>
        <DsrGradeBadge grade={dsr.grade} />
      </div>

      {hasDsr ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="DSR" value={num(dsr.dsr, 3)} accent />
            <Metric label="PSR0(미보정)" value={num(dsr.psr0, 3)} />
            <Metric label="시행 수(N/N_eff)" value={`${dsr.N ?? "-"} / ${num(dsr.N_eff, 1)}`} />
            <Metric label="시행간 상관(ρ̄)" value={num(dsr.rho_bar)} />
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            DSR 은 같은 전략·기간의 파라미터 탐색 시행 수(N_eff)를 반영해 샤프비의
            &quot;운&quot;에 의한 과대추정을 보정한 유의확률이다(Bailey &amp; López de
            Prado 2014). 0.95 이상이면 강한 근거, 0.50 미만이면 과최적화 의심 — 시행
            횟수가 많을수록(파라미터를 많이 튜닝했을수록) 기준이 엄격해진다.
            {dsr.v_low_confidence && (
              <span className="text-amber-600 dark:text-amber-400">
                {" "}시행별 분산 추정의 표본이 적어 참고용으로만 볼 것.
              </span>
            )}
          </p>
        </>
      ) : (
        <p className="text-xs text-muted-foreground">
          {dsr.reason ?? "DSR 을 계산할 수 없습니다."}
          {dsr.psr0 !== null && dsr.psr0 !== undefined && (
            <> (단일시행 기준 PSR0={num(dsr.psr0, 3)})</>
          )}
        </p>
      )}
    </div>
  );
}

/** 팩터 점수 컬럼 → 표시명. score(종합)는 별도 강조 행으로 렌더한다. */
const FACTOR_IC_LABELS: Record<string, string> = {
  score_momentum: "모멘텀",
  score_value: "밸류",
  score_lowvol: "저변동",
  score_quality: "퀄리티",
  score_growth: "성장",
  score: "종합 점수",
};
/** factor_ic 표시 순서(종합은 마지막). */
const FACTOR_IC_ORDER = [
  "score_momentum",
  "score_value",
  "score_lowvol",
  "score_quality",
  "score_growth",
  "score",
];

/** IR 값에 따른 색상(≥1 우수 초록, >0 양호, ≤0 손실 토큰). */
function irColor(ir: number | null | undefined): string {
  if (ir === null || ir === undefined) return "text-muted-foreground";
  if (ir >= 1) return "text-emerald-600 dark:text-emerald-400";
  if (ir > 0) return "text-foreground";
  return "text-loss";
}

/**
 * 팩터 성과귀속·IC/IR 카드(P1-1). 각 팩터 점수의 예측력(IC)·일관성(IR)·방향 적중률·
 * 롱숏 누적수익을 표로 보여준다. method="score" 전략에서만 factor_ic 가 채워진다.
 */
function FactorICCard({ factorIc }: { factorIc: Record<string, FactorIC> }) {
  const rows = FACTOR_IC_ORDER.filter((k) => factorIc[k]);
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h2 className="mb-1 text-sm text-muted-foreground">팩터 성과귀속 · IC/IR</h2>
      <p className="mb-3 text-xs text-muted-foreground">
        각 팩터 점수와 다음 리밸런싱 구간 수익률의 관계. <b>IC</b>=예측력(순위상관),{" "}
        <b>IR</b>=예측 일관성(≥1 우수·&gt;0 양호), <b>적중</b>=방향 적중률,{" "}
        <b>롱숏</b>=상위⅓−하위⅓ 누적수익(이 구간 기여).
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-muted-foreground">
              <th className="py-1 text-left font-normal">팩터</th>
              <th className="py-1 text-right font-normal">IC</th>
              <th className="py-1 text-right font-normal">IR</th>
              <th className="py-1 text-right font-normal">적중</th>
              <th className="py-1 text-right font-normal">롱숏수익</th>
              <th className="py-1 text-right font-normal">n</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((k) => {
              const v = factorIc[k];
              const isTotal = k === "score";
              return (
                <tr
                  key={k}
                  className={`border-b border-border/50 ${isTotal ? "font-semibold" : ""}`}
                >
                  <td className="py-1 text-left">{FACTOR_IC_LABELS[k] ?? k}</td>
                  <td className="py-1 text-right tabular-nums">{num(v.ic_mean, 3)}</td>
                  <td className={`py-1 text-right tabular-nums ${irColor(v.ic_ir)}`}>
                    {num(v.ic_ir)}
                  </td>
                  <td className="py-1 text-right tabular-nums">{pct(v.ic_hit)}</td>
                  <td
                    className={`py-1 text-right tabular-nums ${pctColor(v.ls_return)}`}
                  >
                    {pct(v.ls_return)}
                  </td>
                  <td className="py-1 text-right tabular-nums text-muted-foreground">
                    {v.n}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
