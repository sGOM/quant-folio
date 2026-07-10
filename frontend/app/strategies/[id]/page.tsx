"use client";

import { use, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Backtest, FactorIC, StrategyConfig } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { LineChart } from "@/components/LineChart";
import { RequireAuth } from "@/components/RequireAuth";
import { StrategyForm } from "@/components/StrategyForm";
import { summarizeConfig } from "@/lib/strategy";

/**
 * 비율(0~1)을 백분율 문자열로 변환한다.
 * @param x 비율 값(null/undefined 면 "-")
 * @returns 예: 0.1234 → "12.34%"
 */
function pct(x: number | null | undefined): string {
  if (x === null || x === undefined) return "-";
  return `${(x * 100).toFixed(2)}%`;
}

/**
 * 배수/비율 지표(베타·정보비율 등)를 소수 문자열로 변환한다.
 * @param x 값(null/undefined 면 "-")
 * @param digits 소수 자릿수(기본 2)
 */
function num(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined) return "-";
  return x.toFixed(digits);
}

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
  // 체결 로그: 기본 최근 60건, "더 보기"로 60건씩 추가 노출.
  const TRADE_PAGE = 60;
  const [tradeLimit, setTradeLimit] = useState(TRADE_PAGE);
  // 체결 로그 정렬: 일자(기본, 최근 우선) 또는 종목코드. 헤더 클릭으로 토글.
  const [tradeSort, setTradeSort] = useState<{
    key: "time" | "code";
    dir: "asc" | "desc";
  }>({ key: "time", dir: "desc" });

  const strategy = useQuery({
    queryKey: ["strategy", sid],
    queryFn: () => api.getStrategy(sid),
  });
  // 종목코드 → 한글명 매핑(체결 로그에 종목명 표시). Infinity 는 피한다 — 서버가 최초에
  // 불완전한 맵(외부 소스 일시 실패)을 준 경우 세션 내내 코드만 표시되는 문제를 막고,
  // 서버 자가복구 후 갱신되도록 monitor 와 동일한 유한 staleTime 을 쓴다.
  const names = useQuery({
    queryKey: ["symbol-names"],
    queryFn: api.symbolNames,
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: true,
  });
  // "종목명(코드)" 형태로 표기. 이름을 모르면 코드만 반환.
  const nameOf = (code: string) => {
    const n = names.data?.[code];
    return n ? `${n}(${code})` : code;
  };
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
  // 체결 로그 총 건수(클로저 안에서 안전하게 참조하기 위한 파생 상수).
  const tradeCount = latest?.result?.trades?.length ?? 0;

  // 표시할 체결: 최근 tradeLimit 건을 창으로 잡고, 선택한 기준으로 정렬한다.
  // (limit=최근 몇 건을 볼지, sort=그 창 안의 정렬 순서 — 두 개념을 분리)
  const shownTrades = useMemo(() => {
    const window = (latest?.result?.trades ?? []).slice(-tradeLimit);
    const d = tradeSort.dir === "asc" ? 1 : -1;
    return [...window].sort((a, b) => {
      if (tradeSort.key === "code") {
        const c = String(a.symbol).localeCompare(String(b.symbol));
        // 같은 종목이면 시간 오름차순으로 안정 정렬
        return c !== 0 ? c * d : String(a.t).localeCompare(String(b.t));
      }
      return String(a.t).localeCompare(String(b.t)) * d;
    });
  }, [latest, tradeLimit, tradeSort]);

  // 헤더 클릭: 같은 열이면 방향 토글, 다른 열이면 기본 방향으로 전환
  // (일자=최근 우선 desc, 종목코드=오름차순 asc).
  function toggleTradeSort(key: "time" | "code") {
    setTradeSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "time" ? "desc" : "asc" },
    );
  }
  const sortArrow = (key: "time" | "code") =>
    tradeSort.key === key ? (tradeSort.dir === "asc" ? " ↑" : " ↓") : "";

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
                <div className="rounded-lg border border-border bg-card p-4">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <h2 className="text-sm text-muted-foreground">
                      체결 로그 (매수/매도 · 최근{" "}
                      {Math.min(tradeLimit, tradeCount)}건 / 전체 {tradeCount}건)
                    </h2>
                    <div className="flex shrink-0 gap-1">
                      {tradeLimit < tradeCount && (
                        <button
                          type="button"
                          onClick={() =>
                            setTradeLimit((n) => n + TRADE_PAGE)
                          }
                          className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                        >
                          더 보기 (+
                          {Math.min(TRADE_PAGE, tradeCount - tradeLimit)}건)
                        </button>
                      )}
                      {tradeLimit < tradeCount && (
                        <button
                          type="button"
                          onClick={() => setTradeLimit(tradeCount)}
                          className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                        >
                          전체 보기
                        </button>
                      )}
                      {tradeLimit > TRADE_PAGE && (
                        <button
                          type="button"
                          onClick={() => setTradeLimit(TRADE_PAGE)}
                          className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                        >
                          접기
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="max-h-96 overflow-auto">
                    <table className="w-full text-xs">
                      <thead className="text-muted-foreground">
                        <tr className="border-b border-border">
                          <th className="py-1 text-left font-normal">
                            <button
                              type="button"
                              onClick={() => toggleTradeSort("time")}
                              className="font-normal transition-colors hover:text-foreground"
                              title="일자순 정렬"
                            >
                              일자{sortArrow("time")}
                            </button>
                          </th>
                          <th className="py-1 text-left font-normal">
                            <button
                              type="button"
                              onClick={() => toggleTradeSort("code")}
                              className="font-normal transition-colors hover:text-foreground"
                              title="종목코드순 정렬"
                            >
                              종목{sortArrow("code")}
                            </button>
                          </th>
                          <th className="py-1 text-center font-normal">구분</th>
                          <th className="py-1 text-right font-normal">거래대금</th>
                          <th className="py-1 text-right font-normal">체결가</th>
                          <th className="py-1 text-right font-normal">포지션손익</th>
                        </tr>
                      </thead>
                      <tbody>
                        {shownTrades
                          .map((tr, i) => (
                            <tr key={i} className="border-b border-border/50">
                              <td className="py-1">{tr.t.slice(0, 10)}</td>
                              <td className="py-1">{nameOf(tr.symbol)}</td>
                              <td className="py-1 text-center">
                                <span
                                  className={
                                    tr.side === "buy"
                                      ? "text-red-500"
                                      : tr.reason === "regime_exit" || tr.reason === "mdd_kill"
                                        ? "text-amber-500"
                                        : "text-blue-500"
                                  }
                                >
                                  {tr.side === "buy"
                                    ? "매수"
                                    : tr.reason === "mdd_kill"
                                      ? "킬스위치"
                                      : tr.reason === "regime_exit"
                                        ? "청산"
                                        : "매도"}
                                </span>
                              </td>
                              <td className="py-1 text-right">
                                {tr.amount.toLocaleString()}원
                              </td>
                              <td className="py-1 text-right">
                                {tr.price?.toLocaleString() ?? "-"}
                              </td>
                              <td
                                className={`py-1 text-right ${
                                  (tr.position_return ?? 0) > 0
                                    ? "text-red-500"
                                    : (tr.position_return ?? 0) < 0
                                      ? "text-blue-500"
                                      : ""
                                }`}
                              >
                                {tr.position_return === null ||
                                tr.position_return === undefined
                                  ? "-"
                                  : pct(tr.position_return)}
                              </td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
          </section>
        )}

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

/** IR 값에 따른 색상(≥1 우수 초록, >0 양호, ≤0 빨강). */
function irColor(ir: number | null | undefined): string {
  if (ir === null || ir === undefined) return "text-muted-foreground";
  if (ir >= 1) return "text-emerald-600 dark:text-emerald-400";
  if (ir > 0) return "text-foreground";
  return "text-red-500";
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
                    className={`py-1 text-right tabular-nums ${
                      (v.ls_return ?? 0) > 0
                        ? "text-red-500"
                        : (v.ls_return ?? 0) < 0
                          ? "text-blue-500"
                          : ""
                    }`}
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
