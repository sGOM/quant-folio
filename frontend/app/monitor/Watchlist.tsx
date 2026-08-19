"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Plus, Trash2, TrendingUp } from "lucide-react";
import { api, type Broker } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { formatNumber, formatPercent, trendColor } from "@/lib/format";

/** 워치리스트 localStorage 키. */
const WATCHLIST_KEY = "watchlist:quotes";

/** 해외주식 추천 종목(토스 브로커일 때 빈 목록에 노출). */
const DEFAULT_SUGGEST = ["AAPL", "TSLA", "NVDA"];

/**
 * 해외/국내 종목 실시간 시세 워치리스트.
 * 종목코드를 추가하면 5초마다 시세를 폴링한다. 목록은 localStorage 에 영속한다.
 * 토스 시세 연동(tossQuote) 시 국내+해외를 토스로 통합 조회하고, 아니면 주문 브로커로 조회한다.
 * @param broker    주문 브로커(시세 미연동 시 안내·플레이스홀더 결정)
 * @param tossQuote 통합 시세(토스) 연동 여부
 */
export function Watchlist({
  broker,
  tossQuote,
}: {
  broker?: Broker;
  tossQuote: boolean;
}) {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [input, setInput] = useState("");
  // 복원 완료 전에는 저장 effect 가 초기값([])으로 저장분을 덮어쓰지 않게 가드
  // (AlertCenter 와 동일 패턴 — effect 선언 순서에 대한 암묵적 의존 제거).
  const hydrated = useRef(false);

  // 최초 마운트 시 localStorage 에서 복원.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(WATCHLIST_KEY);
      if (raw) setSymbols(JSON.parse(raw));
    } catch {
      /* 손상된 값 무시 */
    }
    hydrated.current = true;
  }, []);

  // 변경 시 영속(복원 전에는 쓰지 않는다).
  useEffect(() => {
    if (!hydrated.current) return;
    try {
      localStorage.setItem(WATCHLIST_KEY, JSON.stringify(symbols));
    } catch {
      /* quota 초과 등 저장 실패 — 화면 상태는 이미 정상이므로 조용히 무시 */
    }
  }, [symbols]);

  function add(raw: string) {
    const s = raw.trim().toUpperCase();
    if (!s || symbols.includes(s)) return;
    setSymbols((l) => [...l, s]);
    setInput("");
  }
  function remove(s: string) {
    setSymbols((l) => l.filter((x) => x !== s));
  }

  // 토스 시세 연동(tossQuote) 또는 주문 브로커가 토스면 국내+해외 통합 조회가 가능하다.
  const integrated = tossQuote || broker === "toss";

  return (
    <section className="mt-6">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground">
          <TrendingUp className="h-4 w-4" /> 실시간 시세
        </h2>
        <Badge variant={integrated ? "success" : "muted"}>
          {integrated ? "토스 · 국내+해외" : "KIS · 국내전용"}
        </Badge>
      </div>

      <Card className="p-4">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            add(input);
          }}
          className="flex gap-2"
        >
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              integrated
                ? "국내·해외 종목코드 (예: 005930, AAPL)"
                : "국내 종목코드 (예: 005930)"
            }
            className="font-mono"
            autoComplete="off"
          />
          <Button type="submit" size="icon" aria-label="종목 추가">
            <Plus className="h-4 w-4" />
          </Button>
        </form>

        {symbols.length === 0 ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>관심 종목을 추가하세요.</span>
            {integrated &&
              DEFAULT_SUGGEST.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => add(s)}
                  className="rounded-full border px-2 py-0.5 font-mono transition-colors hover:bg-accent"
                >
                  + {s}
                </button>
              ))}
          </div>
        ) : (
          <ul className="mt-3 divide-y divide-border/60">
            {symbols.map((s) => (
              <QuoteRow key={s} symbol={s} onRemove={() => remove(s)} />
            ))}
          </ul>
        )}
      </Card>
    </section>
  );
}

/**
 * 워치리스트 한 행 — 5초 폴링으로 현재가·등락률을 표시한다.
 * 미연동·잘못된 코드 등은 에러 상태로 표시한다(retry 비활성).
 * @param symbol   종목코드
 * @param onRemove 제거 콜백
 */
function QuoteRow({
  symbol,
  onRemove,
}: {
  symbol: string;
  onRemove: () => void;
}) {
  const q = useQuery({
    queryKey: ["quote", symbol],
    queryFn: () => api.quote(symbol),
    refetchInterval: 5000,
    retry: false,
  });

  return (
    <li className="flex items-center justify-between gap-2 py-2">
      <span className="font-mono text-sm font-medium">{symbol}</span>
      <div className="flex items-center gap-3">
        {q.isLoading ? (
          <span className="text-xs text-muted-foreground">조회 중…</span>
        ) : q.isError ? (
          <span className="flex items-center gap-1 text-xs text-destructive">
            <AlertCircle className="h-3.5 w-3.5" /> 조회 실패
          </span>
        ) : q.data ? (
          <>
            <span className="text-sm font-medium tabular-nums">
              {/* 해외주식(USD 등)은 소수 둘째 자리, 원화는 정수로 표시 */}
              {formatNumber(q.data.price, q.data.currency === "KRW" ? 0 : 2)}
              <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                {q.data.currency}
              </span>
            </span>
            <span
              className={cn(
                "text-xs tabular-nums",
                trendColor(q.data.change),
              )}
            >
              {q.data.change > 0 ? "▲" : q.data.change < 0 ? "▼" : "─"}{" "}
              {formatPercent(q.data.change_rate / 100)}
            </span>
          </>
        ) : null}
        <button
          onClick={onRemove}
          aria-label={`${symbol} 제거`}
          className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </li>
  );
}
