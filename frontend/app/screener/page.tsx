"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Search, TrendingUp } from "lucide-react";
import { api, type MetricMarket, type TurnaroundCandidate } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatPercent, trendColor } from "@/lib/format";

function fmtAmt(v: number | null | undefined): string {
  if (v == null) return "-";
  const uk = v / 100_000_000;
  if (uk >= 10_000) return `${(uk / 10_000).toFixed(1)}조`;
  if (uk >= 1) return `${Math.round(uk).toLocaleString("ko-KR")}억`;
  return `${Math.round(v / 10_000).toLocaleString("ko-KR")}만`;
}
function fmtPct(v: number | null | undefined, d = 0): string {
  return v == null ? "-" : formatPercent(v, d);
}

const MARKETS: MetricMarket[] = ["ALL", "KOSDAQ", "KOSPI"];

/** F-Score 배지 색상(0~8): 높을수록 우량. */
function fscoreColor(f: number | null): string {
  if (f == null) return "text-muted-foreground";
  if (f >= 7) return "text-emerald-600 dark:text-emerald-400";
  if (f >= 5) return "text-amber-600 dark:text-amber-400";
  return "text-rose-600 dark:text-rose-400";
}

function StatusChip({ c }: { c: TurnaroundCandidate }) {
  if (c.turnaround === 1)
    return <Badge className="bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">흑자전환</Badge>;
  if ((c.net_income ?? 0) > 0)
    return <Badge variant="secondary">흑자</Badge>;
  return <Badge variant="outline" className="text-muted-foreground">적자</Badge>;
}

export default function ScreenerPage() {
  const [market, setMarket] = useState<MetricMarket>("KOSDAQ");
  const [surge, setSurge] = useState(1.5);
  const [maxDebt, setMaxDebt] = useState(1.5);
  const [run, setRun] = useState(0); // 증가 시 재조회 트리거

  const q = useQuery({
    queryKey: ["screener-turnaround", market, surge, maxDebt, run],
    queryFn: () =>
      api.screenTurnaround({ market, surge, max_debt: maxDebt, top_n: 30 }),
    enabled: run > 0,
    staleTime: 60_000,
    retry: false,
  });

  return (
    <RequireAuth>
      <div className="min-h-screen bg-background">
        <Nav />
        <main className="mx-auto max-w-6xl px-4 py-6 space-y-5">
          <header className="space-y-1">
            <h1 className="flex items-center gap-2 text-xl font-semibold">
              <TrendingUp className="h-5 w-5" /> 소형주 턴어라운드 스크리너
            </h1>
            <p className="text-sm text-muted-foreground">
              전 시장을 스캔해 <b>시총 하위 20% · 거래대금 급증 · 부채비율 한도 이하 ·
              최근 3년 만성적자 제외</b> 필터를 통과한 후보를 발굴합니다. 흑자전환 · 순이익
              YoY · Piotroski F-Score(OpenDART 재무데이터)로 종합점수를 매깁니다.
            </p>
          </header>

          {/* 컨트롤 */}
          <div className="flex flex-wrap items-end gap-3 rounded-md border p-3">
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">시장</span>
              <select
                value={market}
                onChange={(e) => setMarket(e.target.value as MetricMarket)}
                className="h-9 rounded-md border bg-background px-2 text-sm"
              >
                {MARKETS.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">거래대금 급증 배수 ≥</span>
              <input
                type="number" min={1} max={10} step={0.1} value={surge}
                onChange={(e) => setSurge(Number(e.target.value))}
                className="h-9 w-28 rounded-md border bg-background px-2 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">부채비율 상한(배수)</span>
              <input
                type="number" min={0.1} max={10} step={0.1} value={maxDebt}
                onChange={(e) => setMaxDebt(Number(e.target.value))}
                className="h-9 w-28 rounded-md border bg-background px-2 text-sm"
              />
            </label>
            <button
              onClick={() => setRun((n) => n + 1)}
              disabled={q.isFetching}
              className="ml-auto inline-flex h-9 items-center gap-1.5 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              <Search className="h-4 w-4" />
              {q.isFetching ? "스캔 중…" : "스캔 실행"}
            </button>
          </div>

          <p className="text-[11px] text-muted-foreground">
            ⚠️ 전 시장 재무 조회로 <b>수십 초</b> 걸릴 수 있습니다(결과는 6시간 캐시).
            생존편향·슬리피지는 미반영이며 매매 권유가 아닙니다.
          </p>

          {q.isFetching && (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          )}

          {q.isError && (
            <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" /> 스크리너 조회에 실패했습니다.
            </div>
          )}

          {q.data && !q.isFetching && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>기준일 {q.data.as_of}</span>
                <span>· 스캔 후보 {q.data.scanned}</span>
                <span>· 결과 {q.data.count}</span>
                {!q.data.opendart_enabled && (
                  <Badge variant="outline" className="text-amber-600 dark:text-amber-400">
                    OpenDART 키 없음 — 재무 필터 미적용(수급 기준만)
                  </Badge>
                )}
              </div>

              <div className="overflow-x-auto rounded-md border">
                <table className="w-full min-w-[860px] text-sm">
                  <thead className="bg-muted/50 text-xs text-muted-foreground">
                    <tr className="[&>th]:px-3 [&>th]:py-2 [&>th]:text-left">
                      <th>종목</th>
                      <th className="!text-right">시총</th>
                      <th className="!text-right">거래대금 급증</th>
                      <th className="!text-right">부채비율</th>
                      <th className="!text-right">순이익 YoY</th>
                      <th className="!text-center">F-Score</th>
                      <th className="!text-center">상태</th>
                      <th className="!text-right">PBR</th>
                      <th className="!text-right">점수</th>
                    </tr>
                  </thead>
                  <tbody className="[&>tr]:border-t [&>tr>td]:px-3 [&>tr>td]:py-2">
                    {q.data.items.map((c) => (
                      <tr key={c.code} className="hover:bg-muted/30">
                        <td>
                          <div className="font-medium">{c.name}</div>
                          <div className="text-[11px] text-muted-foreground">
                            {c.code} · {c.market}
                          </div>
                        </td>
                        <td className="text-right tabular-nums">{fmtAmt(c.market_cap)}</td>
                        <td className="text-right tabular-nums font-medium">
                          {c.turnover_surge == null ? "-" : `${c.turnover_surge.toFixed(1)}x`}
                        </td>
                        <td className="text-right tabular-nums">
                          {c.debt_ratio == null ? "-" : `${Math.round(c.debt_ratio * 100)}%`}
                        </td>
                        <td className={`text-right tabular-nums ${trendColor(c.net_growth ?? 0)}`}>
                          {fmtPct(c.net_growth)}
                        </td>
                        <td className={`text-center tabular-nums font-semibold ${fscoreColor(c.f_score)}`}>
                          {c.f_score == null ? "-" : `${c.f_score}/8`}
                        </td>
                        <td className="text-center"><StatusChip c={c} /></td>
                        <td className="text-right tabular-nums">
                          {c.pbr == null ? "-" : c.pbr.toFixed(2)}
                        </td>
                        <td className="text-right tabular-nums font-semibold">
                          {c.score.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                    {q.data.items.length === 0 && (
                      <tr>
                        <td colSpan={9} className="px-3 py-8 text-center text-muted-foreground">
                          조건을 만족하는 후보가 없습니다. 급증 배수·부채 상한을 완화해 보세요.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {run === 0 && (
            <div className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
              시장·조건을 정하고 <b>스캔 실행</b>을 누르세요.
            </div>
          )}
        </main>
      </div>
    </RequireAuth>
  );
}
