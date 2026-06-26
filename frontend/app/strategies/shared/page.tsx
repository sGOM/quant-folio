"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Heart, Copy, Loader2, User as UserIcon, Search, X } from "lucide-react";
import { api, SharedStrategy, SharedQuery } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { summarizeConfig } from "@/lib/strategy";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** 입력 공용 스타일(shadcn input 토큰). */
const INPUT =
  "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** 정렬 옵션 라벨. */
const SORT_LABELS: Record<NonNullable<SharedQuery["sort"]>, string> = {
  likes: "좋아요순",
  name: "제목순",
  recent: "최신순",
};

/** 비율(0~1)을 백분율 문자열로 변환. null/undefined 면 "-". */
function pct(x: number | null | undefined): string {
  if (x === null || x === undefined) return "-";
  return `${(x * 100).toFixed(2)}%`;
}

/** 공유 전략 목록 라우트. 인증 게이트로 감싼 콘텐츠를 렌더한다. */
export default function SharedStrategiesPage() {
  return (
    <RequireAuth>
      <SharedContent />
    </RequireAuth>
  );
}

/** 다른 사용자가 공유한 전략을 필터·정렬하고 좋아요·복사할 수 있는 본문. */
function SharedContent() {
  const [q, setQ] = useState("");
  const [symbol, setSymbol] = useState("");
  const [sort, setSort] = useState<NonNullable<SharedQuery["sort"]>>("likes");

  // 입력값을 정규화해 쿼리 파라미터 겸 queryKey 로 사용.
  const params: SharedQuery = {
    q: q.trim() || undefined,
    symbol: symbol.trim() || undefined,
    sort,
  };

  const shared = useQuery({
    queryKey: ["strategies", "shared", params],
    queryFn: () => api.listSharedStrategies(params),
  });

  const hasFilter = !!params.q || !!params.symbol;

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">공유 전략</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            다른 사용자가 공유한 전략을 둘러보고, 마음에 들면 복사해 내 전략으로 저장하세요.
          </p>
        </div>

        {/* 필터·정렬 바 */}
        <div className="mt-5 flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-3">
          <label className="flex-1 space-y-1" style={{ minWidth: 180 }}>
            <span className="block text-xs text-muted-foreground">제목 검색</span>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="전략 제목"
                className={`${INPUT} pl-8`}
              />
            </div>
          </label>
          <label className="space-y-1" style={{ width: 160 }}>
            <span className="block text-xs text-muted-foreground">종목코드</span>
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="예: 005930"
              className={INPUT}
            />
          </label>
          <label className="space-y-1" style={{ width: 130 }}>
            <span className="block text-xs text-muted-foreground">정렬</span>
            <select
              value={sort}
              onChange={(e) =>
                setSort(e.target.value as NonNullable<SharedQuery["sort"]>)
              }
              className={INPUT}
            >
              {(Object.keys(SORT_LABELS) as Array<keyof typeof SORT_LABELS>).map(
                (k) => (
                  <option key={k} value={k}>
                    {SORT_LABELS[k]}
                  </option>
                ),
              )}
            </select>
          </label>
          {hasFilter && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setQ("");
                setSymbol("");
              }}
            >
              <X className="h-4 w-4" /> 필터 초기화
            </Button>
          )}
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          {shared.isLoading &&
            Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-[160px] w-full rounded-lg" />
            ))}
          {shared.data?.length === 0 && (
            <Card className="col-span-full flex flex-col items-center justify-center gap-1 border-dashed py-12 text-center">
              <p className="text-sm font-medium">
                {hasFilter
                  ? "조건에 맞는 공유 전략이 없습니다."
                  : "아직 공유된 전략이 없습니다."}
              </p>
              <p className="text-sm text-muted-foreground">
                {hasFilter
                  ? "필터를 바꾸거나 초기화해 보세요."
                  : "전략 화면에서 공유 버튼을 누르면 여기에 표시됩니다."}
              </p>
            </Card>
          )}
          {shared.data?.map((s) => (
            <SharedCard key={s.id} strategy={s} />
          ))}
        </div>
      </main>
    </>
  );
}

/** 공유 전략 카드. 설명·대표 백테스트 성과를 보여주고 좋아요·복사를 제공한다. */
function SharedCard({ strategy: s }: { strategy: SharedStrategy }) {
  const qc = useQueryClient();
  const router = useRouter();

  const like = useMutation({
    mutationFn: () =>
      s.liked_by_me ? api.unlikeStrategy(s.id) : api.likeStrategy(s.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies", "shared"] }),
  });

  const copy = useMutation({
    mutationFn: () => api.copyStrategy(s.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
      router.push("/strategies");
    },
  });

  return (
    <Card className="flex flex-col p-4">
      <div className="flex-1">
        <p className="font-medium">{s.name}</p>
        <p className="mt-0.5 flex items-center gap-1 text-xs text-muted-foreground">
          <UserIcon className="h-3 w-3" />
          {s.author_name}
          {s.is_mine && <span className="text-primary">(내 전략)</span>}
        </p>
        {s.description && (
          <p className="mt-2 line-clamp-3 whitespace-pre-wrap text-xs text-foreground/80">
            {s.description}
          </p>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          {summarizeConfig(s.config)}
        </p>

        {/* 대표 백테스트 성과 */}
        {s.backtest && (
          <div className="mt-3 rounded-md border border-border bg-muted/30 p-2.5">
            <p className="mb-1 text-[11px] font-medium text-muted-foreground">
              백테스트 {s.backtest.period_start.slice(0, 10)} ~{" "}
              {s.backtest.period_end.slice(0, 10)}
            </p>
            <div className="grid grid-cols-3 gap-2 text-xs">
              <Stat
                label="수익률"
                value={pct(s.backtest.total_return)}
                accent={
                  (s.backtest.total_return ?? 0) >= 0
                    ? "text-profit"
                    : "text-loss"
                }
              />
              <Stat label="MDD" value={pct(s.backtest.mdd)} />
              <Stat
                label="샤프"
                value={s.backtest.sharpe?.toFixed(2) ?? "-"}
              />
            </div>
          </div>
        )}
      </div>

      <div className="mt-3 flex items-center justify-between">
        <button
          onClick={() => like.mutate()}
          disabled={s.is_mine || like.isPending}
          aria-label={s.liked_by_me ? "좋아요 취소" : "좋아요"}
          aria-pressed={s.liked_by_me}
          title={s.is_mine ? "자신의 전략에는 좋아요를 누를 수 없습니다." : undefined}
          className={cn(
            "flex items-center gap-1 rounded-md px-2 py-1 text-sm transition-colors",
            s.is_mine
              ? "cursor-not-allowed text-muted-foreground/50"
              : "hover:bg-accent",
            s.liked_by_me ? "text-rose-500" : "text-muted-foreground",
          )}
        >
          <Heart className={cn("h-4 w-4", s.liked_by_me && "fill-rose-500")} />
          {s.like_count}
        </button>

        <Button
          size="sm"
          variant="secondary"
          onClick={() => copy.mutate()}
          disabled={copy.isPending}
        >
          {copy.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Copy className="h-4 w-4" />
          )}
          복사
        </Button>
      </div>

      {copy.isError && (
        <p className="mt-2 text-xs text-destructive">
          복사 실패: {(copy.error as Error).message}
        </p>
      )}
    </Card>
  );
}

/** 컴팩트 성과 지표(라벨 + 값). */
function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div>
      <p className="text-[10px] text-muted-foreground">{label}</p>
      <p className={cn("font-semibold", accent)}>{value}</p>
    </div>
  );
}
