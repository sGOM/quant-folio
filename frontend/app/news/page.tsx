"use client";

import { useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { AlertCircle, ExternalLink, Newspaper, Search, X } from "lucide-react";
import { api, type NewsItem } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { SymbolSearch } from "@/components/SymbolSearch";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSymbolNames } from "@/lib/useSymbolNames";

const PAGE_SIZE = 30;

export default function NewsPage() {
  return (
    <RequireAuth>
      <NewsContent />
    </RequireAuth>
  );
}

/** 발행시각을 "n분 전 / n시간 전 / 날짜" 로 표기한다. */
function timeLabel(iso: string): string {
  const dt = new Date(iso);
  const diffMin = Math.floor((Date.now() - dt.getTime()) / 60_000);
  if (diffMin < 1) return "방금 전";
  if (diffMin < 60) return `${diffMin}분 전`;
  if (diffMin < 24 * 60) return `${Math.floor(diffMin / 60)}시간 전`;
  return dt.toLocaleDateString("ko-KR", { month: "short", day: "numeric" });
}

function NewsContent() {
  // 종목 필터 — 선택하면 그 종목의 관련 기사만 표시(ROADMAP M3 종목 연계).
  // /news?symbol=005930 딥링크(스크리너 등 다른 화면에서 진입)를 초기값으로 받는다.
  const [symbol, setSymbol] = useState(() =>
    typeof window === "undefined"
      ? ""
      : (new URLSearchParams(window.location.search).get("symbol") ?? ""),
  );
  const { nameOf } = useSymbolNames();

  // 제목 검색 — 입력 즉시가 아니라 300ms 디바운스 후 조회한다(타이핑마다 요청 방지).
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput]);

  // 발행일 범위(KST 기준, 양끝 포함) — 비우면 전체 기간.
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const query = useInfiniteQuery({
    queryKey: ["news", symbol, q, dateFrom, dateTo],
    queryFn: ({ pageParam }) =>
      api.listNews(
        {
          symbol: symbol || undefined,
          q: q || undefined,
          dateFrom: dateFrom || undefined,
          dateTo: dateTo || undefined,
        },
        PAGE_SIZE,
        pageParam,
      ),
    initialPageParam: 0,
    getNextPageParam: (last, all) =>
      last.has_more ? all.length * PAGE_SIZE : undefined,
    staleTime: 5 * 60 * 1000,
  });

  // 새 기사 유입으로 offset 이 밀리면 페이지 경계에서 같은 기사가 중복될 수 있어 id 로 걸러낸다.
  const items: NewsItem[] = (() => {
    const seen = new Set<number>();
    const merged: NewsItem[] = [];
    for (const item of query.data?.pages.flatMap((p) => p.items) ?? []) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        merged.push(item);
      }
    }
    return merged;
  })();

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6">
        <div className="mb-5">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Newspaper className="h-5 w-5 text-primary" />
            경제 뉴스
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            언론사 RSS 에서 매시간 수집한 경제·증시 기사입니다. 요약과 원문 링크만
            제공하며, 종목을 선택하면 관련 기사만 모아 봅니다.
          </p>
        </div>

        {/* 필터 — 종목·제목 검색·발행일 범위 */}
        <div className="mb-6 space-y-2">
          <div className="flex flex-wrap items-end gap-2">
            <div className="w-full max-w-xs">
              <SymbolSearch value={symbol} onChange={setSymbol} />
            </div>
            <div className="relative w-full max-w-xs">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
                placeholder="제목 검색"
                aria-label="제목 검색"
                className="h-9 w-full rounded-md border bg-background pl-8 pr-2 text-sm"
              />
            </div>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">발행일 시작</span>
              <input
                type="date"
                value={dateFrom}
                max={dateTo || undefined}
                onChange={(e) => setDateFrom(e.target.value)}
                className="h-9 rounded-md border bg-background px-2 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">발행일 끝</span>
              <input
                type="date"
                value={dateTo}
                min={dateFrom || undefined}
                onChange={(e) => setDateTo(e.target.value)}
                className="h-9 rounded-md border bg-background px-2 text-sm"
              />
            </label>
          </div>
          {(symbol || q || dateFrom || dateTo) && (
            <div className="flex flex-wrap items-center gap-1.5">
              {symbol && (
                <Badge variant="secondary" className="gap-1">
                  {nameOf(symbol)}
                  <button
                    type="button"
                    aria-label="종목 필터 해제"
                    onClick={() => setSymbol("")}
                    className="ml-0.5 rounded-full hover:text-destructive"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              )}
              {q && (
                <Badge variant="secondary" className="gap-1">
                  “{q}”
                  <button
                    type="button"
                    aria-label="제목 검색 해제"
                    onClick={() => setQInput("")}
                    className="ml-0.5 rounded-full hover:text-destructive"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              )}
              {(dateFrom || dateTo) && (
                <Badge variant="secondary" className="gap-1">
                  {dateFrom || "…"} ~ {dateTo || "…"}
                  <button
                    type="button"
                    aria-label="날짜 필터 해제"
                    onClick={() => {
                      setDateFrom("");
                      setDateTo("");
                    }}
                    className="ml-0.5 rounded-full hover:text-destructive"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              )}
            </div>
          )}
        </div>

        {query.isLoading && (
          <div className="space-y-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-24 w-full rounded-lg" />
            ))}
          </div>
        )}

        {query.isError && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            기사를 불러오지 못했습니다:{" "}
            {query.error instanceof Error ? query.error.message : "알 수 없는 오류"}
          </div>
        )}

        {!query.isLoading && !query.isError && items.length === 0 && (
          <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
            {symbol || q || dateFrom || dateTo
              ? "조건에 맞는 기사가 없습니다. 필터를 완화해 보세요."
              : "수집된 기사가 아직 없습니다. 뉴스는 매시간 자동 수집됩니다."}
          </div>
        )}

        <ul className="space-y-3">
          {items.map((item) => (
            <li
              key={item.id}
              className="rounded-lg border bg-card p-4 transition-colors hover:border-primary/40"
            >
              <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                <span className="font-medium">{item.source}</span>
                <span aria-hidden>·</span>
                <time dateTime={item.published_at}>{timeLabel(item.published_at)}</time>
              </div>
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group inline-flex items-start gap-1.5 font-medium leading-snug hover:text-primary hover:underline"
              >
                {item.title}
                <ExternalLink className="mt-1 h-3.5 w-3.5 shrink-0 text-muted-foreground group-hover:text-primary" />
              </a>
              {item.summary && (
                <p className="mt-1.5 line-clamp-3 text-sm text-muted-foreground">
                  {item.summary}
                </p>
              )}
              {item.symbols.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {item.symbols.map((code) => (
                    <button
                      key={code}
                      type="button"
                      onClick={() => setSymbol(code)}
                      title="이 종목의 관련 기사 보기"
                    >
                      <Badge
                        variant={code === symbol ? "default" : "outline"}
                        className="cursor-pointer hover:bg-accent"
                      >
                        {nameOf(code)}
                      </Badge>
                    </button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>

        {query.hasNextPage && (
          <div className="mt-6 flex justify-center">
            <Button
              variant="outline"
              onClick={() => query.fetchNextPage()}
              disabled={query.isFetchingNextPage}
            >
              {query.isFetchingNextPage ? "불러오는 중…" : "더 보기"}
            </Button>
          </div>
        )}
      </main>
    </>
  );
}
