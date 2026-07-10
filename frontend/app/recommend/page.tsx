"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Info, RotateCcw, Sparkles } from "lucide-react";
import { api, type RecommendMember } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { fmtPct, fmtNum, fmtAmt, pctColor } from "@/lib/format";

// ─────────────────────────── 팩터 정의 ───────────────────────────

/** 슬라이더로 비중을 조절하는 5개 팩터(엔진 _compute_stock_scores 카테고리와 일치). */
const FACTORS = [
  {
    key: "score_momentum",
    id: "momentum",
    label: "모멘텀",
    desc: "최근 상승 추세·52주 고가 근접(강할수록 가점)",
    color: "bg-blue-500",
  },
  {
    key: "score_value",
    id: "value",
    label: "밸류",
    desc: "저PER·저PBR·고배당(쌀수록 가점)",
    color: "bg-emerald-500",
  },
  {
    key: "score_lowvol",
    id: "lowvol",
    label: "저변동",
    desc: "낮은 변동성·얕은 낙폭(안정적일수록 가점)",
    color: "bg-violet-500",
  },
  {
    key: "score_quality",
    id: "quality",
    label: "퀄리티",
    desc: "고ROE·저부채·흑자FCF·F-Score(재무 우량, OpenDART 필요)",
    color: "bg-amber-500",
  },
  {
    key: "score_growth",
    id: "growth",
    label: "성장",
    desc: "영업·순이익 성장·흑자전환(실적 상향, OpenDART 필요)",
    color: "bg-rose-500",
  },
] as const;

type FactorId = (typeof FACTORS)[number]["id"];
type Weights = Record<FactorId, number>;

/** 프리셋 비중(합 100). */
const PRESETS: { label: string; weights: Weights }[] = [
  { label: "균형(기본)", weights: { momentum: 40, value: 30, lowvol: 30, quality: 0, growth: 0 } },
  { label: "모멘텀 중심", weights: { momentum: 60, value: 15, lowvol: 25, quality: 0, growth: 0 } },
  { label: "밸류 중심", weights: { momentum: 20, value: 50, lowvol: 20, quality: 10, growth: 0 } },
  { label: "퀄리티+성장", weights: { momentum: 25, value: 20, lowvol: 15, quality: 25, growth: 15 } },
  { label: "동일 가중", weights: { momentum: 20, value: 20, lowvol: 20, quality: 20, growth: 20 } },
];

const DEFAULT_WEIGHTS: Weights = PRESETS[0].weights;
const TOPN_OPTIONS = [10, 20, 30, 50];

// ─────────────────────────── 페이지 진입점 ───────────────────────────

export default function RecommendPage() {
  return (
    <RequireAuth>
      <RecommendContent />
    </RequireAuth>
  );
}

/** 사용자 비중으로 가중합한 종합점수를 계산해 정렬한 Top-N 결과. */
interface Ranked extends RecommendMember {
  compositeScore: number;
}

function RecommendContent() {
  const [weights, setWeights] = useState<Weights>(DEFAULT_WEIGHTS);
  const [topN, setTopN] = useState(20);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["recommend", "kospi200"],
    queryFn: () => api.getKospi200Recommend(),
    staleTime: 5 * 60 * 1000,
  });

  const total = FACTORS.reduce((s, f) => s + (weights[f.id] || 0), 0);

  // 정규화 비중(합=1). 합이 0이면 균등 배분.
  const normWeights = useMemo<Weights>(() => {
    if (total <= 0) {
      const eq = 1 / FACTORS.length;
      return { momentum: eq, value: eq, lowvol: eq, quality: eq, growth: eq };
    }
    return {
      momentum: weights.momentum / total,
      value: weights.value / total,
      lowvol: weights.lowvol / total,
      quality: weights.quality / total,
      growth: weights.growth / total,
    };
  }, [weights, total]);

  // 종합점수 = Σ 정규화비중 × 팩터별 세부점수(결측은 중립 0). 모멘텀 점수가 없는
  // 종목(가격이력 부족)은 랭킹에서 제외해 엔진 규약과 일치시킨다.
  const ranked = useMemo<Ranked[]>(() => {
    if (!data) return [];
    const rows: Ranked[] = [];
    for (const m of data.items) {
      if (m.score_momentum == null) continue;
      const s =
        normWeights.momentum * (m.score_momentum ?? 0) +
        normWeights.value * (m.score_value ?? 0) +
        normWeights.lowvol * (m.score_lowvol ?? 0) +
        normWeights.quality * (m.score_quality ?? 0) +
        normWeights.growth * (m.score_growth ?? 0);
      rows.push({ ...m, compositeScore: s });
    }
    rows.sort((a, b) => b.compositeScore - a.compositeScore);
    return rows.slice(0, topN);
  }, [data, normWeights, topN]);

  const setWeight = (id: FactorId, v: number) =>
    setWeights((p) => ({ ...p, [id]: Math.max(0, Math.min(100, Math.round(v))) }));

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
        <div className="mb-5">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Sparkles className="h-5 w-5 text-primary" />
            KOSPI200 추천
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            현 시점 KOSPI200 구성종목을 팩터 비중대로 종합점수화해 상위 종목을 추천합니다.
            비중을 조절하면 즉시 재계산됩니다.
          </p>
        </div>

        {/* 비중 컨트롤 패널 */}
        <WeightPanel
          weights={weights}
          total={total}
          onChange={setWeight}
          onPreset={setWeights}
          onReset={() => setWeights(DEFAULT_WEIGHTS)}
        />

        {/* 상단 바: 기준일 · 표시개수 */}
        <div className="mb-4 mt-6 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-sm">
            <span className="text-muted-foreground">표시</span>
            <select
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
              className="h-8 rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {TOPN_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  상위 {n}종목
                </option>
              ))}
            </select>
          </label>
          {data && (
            <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
              <Info className="h-3.5 w-3.5" />
              기준일: {data.as_of.slice(0, 10)} · {data.index} {data.count}종목
            </span>
          )}
        </div>

        {/* OpenDART 미연동 경고(퀄리티·성장 팩터 사용 시) */}
        {data &&
          !data.opendart_enabled &&
          (weights.quality > 0 || weights.growth > 0) && (
            <div className="mb-4 flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                재무데이터(OpenDART)가 연동되지 않아 <b>퀄리티·성장</b> 팩터가 모든 종목에서
                중립(0) 처리됩니다. 두 비중은 결과에 영향을 주지 않습니다.
              </span>
            </div>
          )}

        <div className="mb-4 flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            종합점수는 KOSPI200 종목군 내 상대 우위를 표준화(z-score)한 뒤 비중대로 가중합한
            값입니다(0 이상이면 평균 이상). 절대 수익률이 아니며, 투자 판단의 참고 지표입니다.
          </span>
        </div>

        {isLoading && (
          <div className="space-y-2">
            {Array.from({ length: 10 }).map((_, i) => (
              <Skeleton key={i} className="h-11 w-full rounded-md" />
            ))}
          </div>
        )}
        {isError && (
          <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 py-12 text-center">
            <AlertCircle className="h-8 w-8 text-destructive" />
            <p className="text-sm font-medium text-destructive">데이터를 불러오지 못했습니다</p>
            <p className="text-xs text-muted-foreground">{(error as Error).message}</p>
          </div>
        )}
        {data && ranked.length === 0 && !isLoading && (
          <div className="flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed py-12 text-center">
            <p className="text-sm font-medium text-muted-foreground">
              추천 종목이 없습니다. KRX 인증·데이터 상태를 확인하세요.
            </p>
          </div>
        )}
        {ranked.length > 0 && <ResultTable rows={ranked} weights={normWeights} />}
      </main>
    </>
  );
}

// ─────────────────────────── 비중 컨트롤 패널 ───────────────────────────

function WeightPanel({
  weights,
  total,
  onChange,
  onPreset,
  onReset,
}: {
  weights: Weights;
  total: number;
  onChange: (id: FactorId, v: number) => void;
  onPreset: (w: Weights) => void;
  onReset: () => void;
}) {
  const balanced = total === 100;
  return (
    <div className="rounded-lg border bg-card p-4 sm:p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">팩터 비중 (합계 100)</h2>
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "rounded-md px-2 py-1 text-xs font-medium tabular-nums",
              balanced
                ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
            )}
          >
            합계 {total}
            {!balanced && " (자동 정규화됨)"}
          </span>
          <Button variant="ghost" size="sm" onClick={onReset} className="text-muted-foreground">
            <RotateCcw className="h-3.5 w-3.5" />
            초기화
          </Button>
        </div>
      </div>

      {/* 프리셋 */}
      <div className="mb-4 flex flex-wrap gap-1.5">
        {PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => onPreset(p.weights)}
            className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* 슬라이더 */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
        {FACTORS.map((f) => {
          const v = weights[f.id] || 0;
          const pct = total > 0 ? (v / total) * 100 : 0;
          return (
            <div key={f.id} className="flex items-center gap-3">
              <div className="w-40 shrink-0">
                <div className="flex items-center gap-1.5">
                  <span className={cn("h-2.5 w-2.5 rounded-sm", f.color)} />
                  <span className="text-sm font-medium">{f.label}</span>
                </div>
                <p className="mt-0.5 truncate text-[11px] text-muted-foreground" title={f.desc}>
                  {f.desc}
                </p>
              </div>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={v}
                onChange={(e) => onChange(f.id, Number(e.target.value))}
                className="h-1.5 flex-1 cursor-pointer accent-primary"
                aria-label={`${f.label} 비중`}
              />
              <input
                type="number"
                min={0}
                max={100}
                value={v}
                onChange={(e) => onChange(f.id, Number(e.target.value))}
                className="h-8 w-16 rounded-md border border-input bg-input px-2 text-right text-sm tabular-nums text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                aria-label={`${f.label} 비중 입력`}
              />
              <span className="w-12 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                {pct.toFixed(0)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─────────────────────────── 결과 테이블 ───────────────────────────

function ScoreBadge({ score }: { score: number }) {
  const color =
    score >= 0.5
      ? "text-profit font-semibold"
      : score >= 0
        ? "text-foreground font-medium"
        : "text-muted-foreground";
  return <span className={cn("tabular-nums", color)}>{score.toFixed(2)}</span>;
}

/** 세부점수 미니 셀(음/양에 따라 옅은 색). */
function SubScore({ v }: { v: number | null }) {
  if (v == null) return <span className="text-muted-foreground/40">·</span>;
  return (
    <span
      className={cn(
        "tabular-nums",
        v >= 0.3 ? "text-profit" : v <= -0.3 ? "text-loss" : "text-muted-foreground",
      )}
    >
      {v.toFixed(1)}
    </span>
  );
}

function ResultTable({
  rows,
  weights,
}: {
  rows: Ranked[];
  weights: Weights;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[1080px] text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            <th className="w-10 px-2 py-2.5 text-center font-medium">#</th>
            <th className="sticky left-0 z-10 bg-muted/50 px-3 py-2.5 text-left font-medium">
              종목
            </th>
            <th className="px-3 py-2.5 text-right font-medium">현재가</th>
            <th className="px-3 py-2.5 text-right font-medium">등락률</th>
            <th className="px-3 py-2.5 text-right font-medium">시총</th>
            <th className="px-3 py-2.5 text-right font-medium">PER</th>
            <th className="px-3 py-2.5 text-right font-medium">PBR</th>
            <th className="px-3 py-2.5 text-right font-medium">6M</th>
            <th className="px-3 py-2.5 text-right font-medium">변동성</th>
            {/* 세부점수 그룹 */}
            {FACTORS.map((f) => (
              <th
                key={f.id}
                className={cn(
                  "px-2 py-2.5 text-right font-medium",
                  (weights[f.id] || 0) === 0 && "opacity-40",
                )}
                title={`${f.label} 세부점수`}
              >
                {f.label.slice(0, 2)}
              </th>
            ))}
            <th className="px-3 py-2.5 text-right font-medium">종합점수</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m, i) => (
            <tr
              key={m.code}
              className="border-t border-border/60 transition-colors hover:bg-accent/30"
            >
              <td className="px-2 py-2 text-center tabular-nums text-muted-foreground">
                {i + 1}
              </td>
              <td className="sticky left-0 bg-background px-3 py-2 hover:bg-accent/30">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium leading-tight">{m.name}</span>
                  {m.trend_aligned && (
                    <Badge variant="success" className="text-[9px]">
                      정배열
                    </Badge>
                  )}
                </div>
                <div className="font-mono text-[11px] text-muted-foreground">{m.code}</div>
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {m.price ? m.price.toLocaleString("ko-KR") : "-"}
              </td>
              <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(m.change_rate))}>
                {m.change_rate == null
                  ? "-"
                  : `${m.change_rate > 0 ? "▲" : m.change_rate < 0 ? "▼" : "─"} ${fmtPct(m.change_rate)}`}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                {fmtAmt(m.market_cap)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                {fmtNum(m.per, 1)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                {fmtNum(m.pbr, 2)}
              </td>
              <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(m.mom_6m))}>
                {fmtPct(m.mom_6m)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                {fmtPct(m.vol_ann)}
              </td>
              {/* 세부점수 */}
              <td className={cn("px-2 py-2 text-right", (weights.momentum || 0) === 0 && "opacity-40")}>
                <SubScore v={m.score_momentum} />
              </td>
              <td className={cn("px-2 py-2 text-right", (weights.value || 0) === 0 && "opacity-40")}>
                <SubScore v={m.score_value} />
              </td>
              <td className={cn("px-2 py-2 text-right", (weights.lowvol || 0) === 0 && "opacity-40")}>
                <SubScore v={m.score_lowvol} />
              </td>
              <td className={cn("px-2 py-2 text-right", (weights.quality || 0) === 0 && "opacity-40")}>
                <SubScore v={m.score_quality} />
              </td>
              <td className={cn("px-2 py-2 text-right", (weights.growth || 0) === 0 && "opacity-40")}>
                <SubScore v={m.score_growth} />
              </td>
              <td className="px-3 py-2 text-right">
                <ScoreBadge score={m.compositeScore} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
