"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Play,
  Square,
  Loader2,
  Plus,
  Trash2,
  TrendingUp,
  AlertCircle,
  Building2,
  Globe,
} from "lucide-react";
import { api, Strategy, BROKER_INFO, type Broker } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { useEventSocket } from "@/lib/useWebSocket";
import { summarizeConfig } from "@/lib/strategy";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { formatNumber, formatPercent, trendColor } from "@/lib/format";

// 매매·체결 관련 이벤트 타입 — 어느 것이든 잔고/주문을 다시 가져온다.
const TRADE_EVENTS = new Set(["execution", "order", "position", "fill", "signal"]);

/** 실시간 이벤트 로그 한 줄. id 는 안정적인 React key 용 단조 증가 값. */
interface LogEntry {
  id: number;
  text: string;
}

/** 실시간 모니터링 라우트. 인증 게이트로 감싼 콘텐츠를 렌더한다. */
export default function MonitorPage() {
  return (
    <RequireAuth>
      <MonitorContent />
    </RequireAuth>
  );
}

/**
 * 실시간 모니터링 본문.
 * 전략 ON/OFF, 보유 포지션, 실시간 이벤트 로그, 주문 감사 로그를 표시한다.
 * WS 이벤트로 즉시 갱신하되, 누락 대비 15초 폴백 폴링을 병행한다.
 */
function MonitorContent() {
  const qc = useQueryClient();
  const [log, setLog] = useState<LogEntry[]>([]);
  const logSeq = useRef(0);

  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: api.listStrategies });
  // WS 이벤트를 놓쳐도 화면이 영구히 stale 되지 않도록 보수적 폴백 폴링.
  const positions = useQuery({
    queryKey: ["positions"],
    queryFn: api.positions,
    refetchInterval: 15000,
  });
  const orders = useQuery({
    queryKey: ["orders"],
    queryFn: api.orders,
    refetchInterval: 15000,
  });
  const engine = useQuery({
    queryKey: ["engine-status"],
    queryFn: api.engineStatus,
    refetchInterval: 10000,
  });

  // 실시간 이벤트 → 쿼리 무효화 + 로그
  useEventSocket((data) => {
    const type = data.type as string;
    if (TRADE_EVENTS.has(type)) {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["strategies"] });
      const t = new Date().toLocaleTimeString("ko-KR");
      const f = (v: unknown) => (v == null ? "" : String(v));
      const desc =
        type === "execution"
          ? `체결 ${f(data.side)} ${f(data.symbol)} ${f(data.qty)}주 @ ${f(data.price)}`
          : type === "order"
            ? `주문 ${f(data.status)} ${f(data.symbol)}`
            : `${type} ${f(data.symbol)}`;
      const id = logSeq.current++;
      setLog((l) => [{ id, text: `${t}  ${desc}` }, ...l].slice(0, 30));
    }
  });

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">실시간 모니터링</h1>
            {me.data && (
              <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
                {me.data.broker === "toss" ? (
                  <Globe className="h-3.5 w-3.5" />
                ) : (
                  <Building2 className="h-3.5 w-3.5" />
                )}
                주문 브로커:{" "}
                <span className="font-medium text-foreground">
                  {BROKER_INFO[me.data.broker].name}
                </span>
                <span className="text-xs">
                  ({BROKER_INFO[me.data.broker].market})
                </span>
              </p>
            )}
          </div>
          <span
            className={cn(
              "flex items-center gap-2 rounded-full border px-3 py-1 text-sm font-medium",
              engine.data?.engine_alive
                ? "border-profit/30 bg-profit/10 text-profit"
                : "border-loss/30 bg-loss/10 text-loss",
            )}
          >
            <span className="relative flex h-2 w-2">
              {engine.data?.engine_alive && (
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-profit opacity-75" />
              )}
              <span
                className={cn(
                  "relative inline-flex h-2 w-2 rounded-full",
                  engine.data?.engine_alive ? "bg-profit" : "bg-loss",
                )}
              />
            </span>
            매매 엔진 {engine.data?.engine_alive ? "동작 중" : "중지"}
          </span>
        </div>

        <Watchlist
          broker={me.data?.broker}
          tossQuote={!!me.data?.has_toss_quote}
        />

        <section className="mt-8">
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">
            전략 ON/OFF
          </h2>
          <div className="space-y-2">
            {strategies.data?.map((s) => (
              <StrategyToggle key={s.id} strategy={s} />
            ))}
            {strategies.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">전략이 없습니다.</p>
            )}
          </div>
        </section>

        <div className="mt-8 grid gap-6 lg:grid-cols-2">
          <section>
            <h2 className="mb-2 text-sm font-medium text-muted-foreground">
              보유 포지션
            </h2>
            <Table
              head={["종목", "수량", "평균단가"]}
              rows={
                positions.data?.map((p) => [
                  p.symbol,
                  p.qty.toLocaleString(),
                  p.avg_price.toLocaleString(),
                ]) ?? []
              }
              empty="보유 포지션이 없습니다."
            />
          </section>

          <section>
            <h2 className="mb-2 text-sm font-medium text-muted-foreground">
              실시간 이벤트
            </h2>
            <div className="h-48 overflow-auto rounded-lg border bg-card p-3 font-mono text-xs text-muted-foreground">
              {log.length === 0 ? (
                <p className="text-muted-foreground/60">이벤트 대기 중…</p>
              ) : (
                log.map((l) => (
                  <div
                    key={l.id}
                    className="animate-fade-in border-b border-border/40 py-1 last:border-0"
                  >
                    {l.text}
                  </div>
                ))
              )}
            </div>
          </section>
        </div>

        <section className="mt-8">
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">
            최근 주문 (감사 로그)
          </h2>
          <Table
            head={["시각", "종목", "구분", "수량", "상태"]}
            rows={
              orders.data?.map((o) => [
                new Date(o.created_at).toLocaleString("ko-KR"),
                o.symbol,
                o.side === "buy" ? "매수" : "매도",
                o.qty.toLocaleString(),
                o.status,
              ]) ?? []
            }
            empty="주문 내역이 없습니다."
          />
        </section>
      </main>
    </>
  );
}

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
function Watchlist({
  broker,
  tossQuote,
}: {
  broker?: Broker;
  tossQuote: boolean;
}) {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [input, setInput] = useState("");

  // 최초 마운트 시 localStorage 에서 복원.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(WATCHLIST_KEY);
      if (raw) setSymbols(JSON.parse(raw));
    } catch {
      /* 손상된 값 무시 */
    }
  }, []);

  // 변경 시 영속.
  useEffect(() => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(symbols));
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

/**
 * 전략별 시작/중지 토글. 현재 상태(live 여부)에 따라 엔진 start/stop 을 호출하고,
 * 성공 시 전략 목록을 무효화해 상태 배지를 갱신한다.
 * @param strategy 토글 대상 전략
 */
function StrategyToggle({ strategy }: { strategy: Strategy }) {
  const qc = useQueryClient();
  const isLive = strategy.status === "live";
  const toggle = useMutation({
    mutationFn: () =>
      isLive ? api.stopStrategy(strategy.id) : api.startStrategy(strategy.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  return (
    <Card className="flex items-center justify-between px-4 py-3">
      <div className="flex items-center gap-3">
        <span
          className={cn(
            "h-2 w-2 shrink-0 rounded-full",
            isLive ? "bg-profit" : "bg-muted-foreground/40",
          )}
        />
        <div>
          <p className="text-sm font-medium">{strategy.name}</p>
          <p className="text-xs text-muted-foreground">
            {summarizeConfig(strategy.config)}
          </p>
        </div>
      </div>
      <Button
        size="sm"
        variant={isLive ? "destructive" : "default"}
        onClick={() => toggle.mutate()}
        disabled={toggle.isPending}
        className={cn(!isLive && "bg-profit text-white hover:bg-profit/90")}
      >
        {toggle.isPending ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : isLive ? (
          <>
            <Square className="h-3.5 w-3.5" /> 중지
          </>
        ) : (
          <>
            <Play className="h-3.5 w-3.5" /> 시작
          </>
        )}
      </Button>
    </Card>
  );
}

/**
 * 단순 표 컴포넌트.
 * @param head  헤더 셀 라벨 배열
 * @param rows  행 배열(각 행은 셀 값 배열)
 * @param empty 행이 없을 때 표시할 안내 문구
 */
function Table({
  head,
  rows,
  empty,
}: {
  head: string[];
  rows: (string | number)[][];
  empty: string;
}) {
  return (
    <div className="overflow-hidden rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-3 py-2 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td
                colSpan={head.length}
                className="px-3 py-6 text-center text-muted-foreground"
              >
                {empty}
              </td>
            </tr>
          ) : (
            rows.map((r, i) => (
              <tr
                key={i}
                className="border-t border-border/60 transition-colors hover:bg-accent/30"
              >
                {r.map((c, j) => (
                  <td key={j} className="px-3 py-2 tabular-nums">
                    {c}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
