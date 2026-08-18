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
  HelpCircle,
  HeartPulse,
  Star,
} from "lucide-react";
import {
  api,
  ApiError,
  Strategy,
  BROKER_INFO,
  type Broker,
  type OrderRow,
  type StrategyHealth,
  type FillQualityReport,
  type CalibrationProposal,
} from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { useCurrentUser } from "@/lib/useAuth";
import { useSymbolNames } from "@/lib/useSymbolNames";
import { useEventSocket } from "@/lib/useWebSocket";
import { summarizeConfig } from "@/lib/strategy";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { formatKRW, formatNumber, formatPercent, formatRelativeTime, trendColor } from "@/lib/format";

// 매매·체결 관련 이벤트 타입 — 어느 것이든 잔고/주문을 다시 가져온다.
const TRADE_EVENTS = new Set(["execution", "order", "position", "fill", "signal"]);

/** 주문 상태별 표시 라벨·색조(초록=진행/체결, 빨강=거부/취소)·툴팁 설명.
 *  tone 은 운영 상태 축이다 — 손익색(profit=적/loss=청)이 아니라 신호등 규칙(ok=초록/bad=적). */
const ORDER_STATUS: Record<
  string,
  { label: string; tone: "ok" | "bad" | "muted"; desc: string }
> = {
  pending: {
    label: "대기",
    tone: "muted",
    desc: "엔진이 주문을 생성했고 증권사(KIS/토스)로 전송하기 직전 상태입니다.",
  },
  submitted: {
    label: "접수",
    tone: "ok",
    desc: "증권사가 주문을 정상 접수했습니다. 아직 체결 전이며 곧 체결·미체결이 확정됩니다.",
  },
  partial: {
    label: "일부체결",
    tone: "ok",
    desc: "주문 수량 중 일부만 체결되었습니다. 나머지 수량은 계속 체결을 기다립니다.",
  },
  filled: {
    label: "체결완료",
    tone: "ok",
    desc: "주문 수량이 전량 체결되어 포지션·잔고에 반영되었습니다.",
  },
  cancelled: {
    label: "취소",
    tone: "bad",
    desc: "주문이 취소되어 체결되지 않았습니다.",
  },
  rejected: {
    label: "거부",
    tone: "bad",
    desc: "증권사가 주문을 거부했습니다(잔고 부족·호가 이탈·장 상태 등). 체결되지 않았습니다.",
  },
};

/** DB 백업 신선도 판정 임계(시간) — worker.check_backup_freshness(§9)와 동일 기준. */
const BACKUP_STALE_HOURS = 26;

/** 백업 신선도 배지 톤 판정. null(백업 이력 없음)·임계 초과는 위험, 그 외 정상. */
function backupFreshnessTone(lastSuccessAt: string | null | undefined): "ok" | "bad" | "unknown" {
  if (lastSuccessAt === undefined) return "unknown";
  if (lastSuccessAt === null) return "bad";
  const hoursSince = (Date.now() - new Date(lastSuccessAt).getTime()) / 3_600_000;
  return hoursSince > BACKUP_STALE_HOURS ? "bad" : "ok";
}

/** 매매 엔진 상태 배지에 붙일 상세 설명(툴팁). */
function engineStatusDesc(loading: boolean, alive?: boolean): string {
  if (loading) return "매매 엔진의 가동 여부를 확인하는 중입니다.";
  if (alive)
    return (
      "24시간 자동매매 데몬(engine)이 이벤트 루프를 돌며 시세 수신·신호 평가·주문 실행을 " +
      "정상 처리 중입니다. 활성 전략의 매매가 실시간으로 수행됩니다."
    );
  return (
    "매매 엔진 데몬이 가동되지 않고 있습니다. 전략을 ON 으로 두어도 실제 주문이 나가지 " +
    "않으니, 관리자에게 엔진 프로세스 상태를 확인하세요."
  );
}

/**
 * 실시간 이벤트 로그 한 줄. id 는 안정적인 React key 용 단조 증가 값.
 * raw 는 WS 로 수신한 이벤트 페이로드 원문 — 요약(text)에는 side/symbol/qty/price/status
 * 등 핵심 필드만 담기므로, 상세 진단이 필요하면 접기 UI로 원본 전체를 펼쳐 본다(§16).
 */
interface LogEntry {
  id: number;
  text: string;
  raw: Record<string, unknown>;
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
  // 체결 정합 리포트 대상 전략. undefined="전체"(계정 통합) — 슬리피지 캘리브레이션은
  // 전략별 config 를 바꾸는 조치라, 특정 전략을 골랐을 때만 승인 UI 를 노출한다.
  const [fillQualityStrategyId, setFillQualityStrategyId] = useState<number | undefined>(
    undefined,
  );
  // 전략 ON/OFF 목록을 즐겨찾기 전략만으로 좁혀 보는 토글.
  const [favoritesOnly, setFavoritesOnly] = useState(false);

  // 인증 쿼리는 useCurrentUser 로 통일(retry:false 등 옵션이 화면마다 갈리지 않게).
  const me = useCurrentUser();
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: api.listStrategies });
  // 종목코드 → 한글명 매핑(로그·표에 종목명 표시). 캐시 정책은 훅 참조.
  const { nameOf } = useSymbolNames();
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
  // 전략별 러너 헬스(마지막 성공 틱·연속 실패). 엔진 전역 생존과 별개로 러너 단위 이상을 감지.
  const health = useQuery({
    queryKey: ["strategies-health"],
    queryFn: api.getStrategiesHealth,
    refetchInterval: 30000,
  });
  // 체결 정합 실측(P2-3, D-2) — 최근 90일 실거래 체결이 백테스트 슬리피지 가정과
  // 얼마나 어긋나는지. 매매가 드문 계정은 표본 부족(INSUFFICIENT)이 정상 경로.
  const fillQuality = useQuery({
    queryKey: ["fill-quality", fillQualityStrategyId ?? "all"],
    queryFn: () => api.getFillQuality(90, fillQualityStrategyId),
    staleTime: 5 * 60 * 1000,
  });

  // 실시간 이벤트 → 쿼리 무효화 + 로그
  useEventSocket((data) => {
    const type = data.type as string;
    if (type === "alert") {
      // 러너 실패·MDD 킬 등은 헬스 위젯을 즉시 갱신(30초 폴링을 기다리지 않음).
      qc.invalidateQueries({ queryKey: ["strategies-health"] });
      return;
    }
    if (TRADE_EVENTS.has(type)) {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["strategies"] });
      const t = new Date().toLocaleTimeString("ko-KR");
      const f = (v: unknown) => (v == null ? "" : String(v));
      const desc =
        type === "execution"
          ? `체결 ${f(data.side)} ${nameOf(data.symbol as string)} ${f(data.qty)}주 @ ${f(data.price)}`
          : type === "order"
            ? `주문 ${f(data.status)} ${nameOf(data.symbol as string)}`
            : `${type} ${nameOf(data.symbol as string)}`;
      const id = logSeq.current++;
      setLog((l) => [{ id, text: `${t}  ${desc}`, raw: data }, ...l].slice(0, 30));
    }
  });

  return (
    <TooltipProvider delayDuration={150}>
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
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                tabIndex={0}
                className={cn(
                  "flex cursor-help items-center gap-2 rounded-full border px-3 py-1 text-sm font-medium",
                  // 첫 응답 전(isLoading)엔 engine_alive 가 undefined(falsy) 라 실제 가동 중이어도
                  // 적색 '중지'로 오표시된다. 로딩 중엔 중립(muted) 상태로 분기한다.
                  engine.isLoading
                    ? "border-border bg-muted text-muted-foreground"
                    : engine.data?.engine_alive
                      ? "border-status-ok/30 bg-status-ok/10 text-status-ok"
                      : "border-status-bad/30 bg-status-bad/10 text-status-bad",
                )}
              >
                <span className="relative flex h-2 w-2">
                  {engine.data?.engine_alive && !engine.isLoading && (
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-ok opacity-75" />
                  )}
                  <span
                    className={cn(
                      "relative inline-flex h-2 w-2 rounded-full",
                      engine.isLoading
                        ? "bg-muted-foreground"
                        : engine.data?.engine_alive
                          ? "bg-status-ok"
                          : "bg-status-bad",
                    )}
                  />
                </span>
                매매 엔진{" "}
                {engine.isLoading ? "확인 중…" : engine.data?.engine_alive ? "동작 중" : "중지"}
                <HelpCircle className="h-3.5 w-3.5 opacity-60" />
              </span>
            </TooltipTrigger>
            <TooltipContent>
              {engineStatusDesc(engine.isLoading, engine.data?.engine_alive)}
            </TooltipContent>
          </Tooltip>
        </div>

        {!engine.isLoading && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                tabIndex={0}
                className={cn(
                  "mt-2 flex w-fit cursor-help items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium",
                  backupFreshnessTone(engine.data?.backup_last_success_at) === "ok"
                    ? "border-status-ok/30 bg-status-ok/10 text-status-ok"
                    : "border-status-bad/30 bg-status-bad/10 text-status-bad",
                )}
              >
                DB 백업{" "}
                {engine.data?.backup_last_success_at
                  ? `마지막 성공 ${formatRelativeTime(engine.data.backup_last_success_at)}`
                  : "성공 이력 없음"}
                <HelpCircle className="h-3 w-3 opacity-60" />
              </span>
            </TooltipTrigger>
            <TooltipContent>
              worker 컨테이너가 매일 새벽 03:00 KST 에 DB 를 백업합니다(§9). 마지막 성공이
              {BACKUP_STALE_HOURS}시간을 넘으면 백업이 밀렸다는 뜻이므로 worker 로그와
              Celery beat 스케줄을 확인하세요.
            </TooltipContent>
          </Tooltip>
        )}

        <Watchlist
          broker={me.data?.broker}
          tossQuote={!!me.data?.has_toss_quote}
        />

        <section className="mt-8">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-medium text-muted-foreground">
              전략 ON/OFF
            </h2>
            <button
              type="button"
              onClick={() => setFavoritesOnly((v) => !v)}
              aria-pressed={favoritesOnly}
              className={cn(
                "flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                favoritesOnly
                  ? "border-amber-400/40 bg-amber-400/10 text-amber-500"
                  : "border-border text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <Star
                className={cn("h-3.5 w-3.5", favoritesOnly && "fill-amber-400 text-amber-400")}
              />
              즐겨찾기만
            </button>
          </div>
          <div className="space-y-2">
            {(favoritesOnly
              ? strategies.data?.filter((s) => s.is_favorite)
              : strategies.data
            )?.map((s) => <StrategyToggle key={s.id} strategy={s} />)}
            {strategies.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">전략이 없습니다.</p>
            )}
            {favoritesOnly &&
              strategies.data &&
              strategies.data.length > 0 &&
              !strategies.data.some((s) => s.is_favorite) && (
                <p className="text-sm text-muted-foreground">
                  즐겨찾기한 전략이 없습니다. 전략 메뉴에서 별표를 눌러 추가하세요.
                </p>
              )}
          </div>
        </section>

        <section className="mt-8">
          <h2 className="mb-2 flex items-center gap-1.5 text-sm font-medium text-muted-foreground">
            <HeartPulse className="h-4 w-4" /> 전략 러너 헬스
          </h2>
          <StrategyHealthPanel
            data={health.data}
            isLoading={health.isLoading}
            isError={health.isError}
          />
        </section>

        <section className="mt-8">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-medium text-muted-foreground">
              체결 정합 실측 (최근 90일)
            </h2>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              대상 전략
              <select
                value={fillQualityStrategyId ?? "all"}
                onChange={(e) =>
                  setFillQualityStrategyId(
                    e.target.value === "all" ? undefined : Number(e.target.value),
                  )
                }
                className="h-8 rounded-md border border-input bg-transparent px-2 text-xs shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <option value="all">전체(계정 통합)</option>
                {strategies.data?.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <FillQualityPanel
            data={fillQuality.data}
            isLoading={fillQuality.isLoading}
            isError={fillQuality.isError}
            strategyId={fillQualityStrategyId}
          />
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
                  nameOf(p.symbol),
                  formatNumber(p.qty),
                  formatKRW(p.avg_price, false),
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
                  <details
                    key={l.id}
                    className="animate-fade-in border-b border-border/40 py-1 last:border-0"
                  >
                    <summary className="cursor-pointer list-none">{l.text}</summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-muted/40 p-1.5 text-[10px] leading-relaxed text-muted-foreground/80">
                      {JSON.stringify(l.raw, null, 2)}
                    </pre>
                  </details>
                ))
              )}
            </div>
          </section>
        </div>

        <section className="mt-8">
          <h2 className="mb-2 text-sm font-medium text-muted-foreground">
            최근 주문 (감사 로그)
          </h2>
          <OrderAuditTable orders={orders.data} nameOf={nameOf} />
        </section>
      </main>
    </TooltipProvider>
  );
}

/**
 * 주문 감사 로그 테이블.
 * 각 주문이 '어떤 신호·공식·리스크·리밸런싱 기준'으로 나갔는지(reason)를 상세 열로 보여주고,
 * 상태는 색조 배지(초록=진행/체결, 빨강=거부/취소)와 툴팁 설명으로 표기한다.
 */
function OrderAuditTable({
  orders,
  nameOf,
}: {
  orders?: OrderRow[];
  nameOf: (code?: string | null) => string;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[52rem] text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            {["시각", "종목", "구분", "수량", "상태", "사유"].map((h) => (
              <th key={h} className="px-3 py-2 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!orders || orders.length === 0 ? (
            <tr>
              <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                주문 내역이 없습니다.
              </td>
            </tr>
          ) : (
            orders.map((o) => {
              const st = ORDER_STATUS[o.status] ?? {
                label: o.status,
                tone: "muted" as "ok" | "bad" | "muted",
                desc: "알 수 없는 상태입니다.",
              };
              return (
                <tr
                  key={o.id}
                  className="border-t border-border/60 align-top transition-colors hover:bg-accent/30"
                >
                  <td className="whitespace-nowrap px-3 py-2 tabular-nums text-muted-foreground">
                    {new Date(o.created_at).toLocaleString("ko-KR")}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2">{nameOf(o.symbol)}</td>
                  <td className="px-3 py-2">
                    <span
                      className={cn(
                        "font-medium",
                        o.side === "buy" ? "text-profit" : "text-loss",
                      )}
                    >
                      {o.side === "buy" ? "매수" : "매도"}
                    </span>
                  </td>
                  <td className="px-3 py-2 tabular-nums">{formatNumber(o.qty)}</td>
                  <td className="px-3 py-2">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span
                          tabIndex={0}
                          className={cn(
                            "inline-flex cursor-help items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
                            st.tone === "ok"
                              ? "border-status-ok/30 bg-status-ok/10 text-status-ok"
                              : st.tone === "bad"
                                ? "border-status-bad/30 bg-status-bad/10 text-status-bad"
                                : "border-border bg-muted text-muted-foreground",
                          )}
                        >
                          {st.label}
                          <HelpCircle className="h-3 w-3 opacity-60" />
                        </span>
                      </TooltipTrigger>
                      <TooltipContent>{st.desc}</TooltipContent>
                    </Tooltip>
                  </td>
                  <td className="px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                    {o.reason ?? (
                      <span className="text-muted-foreground/50">—</span>
                    )}
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
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
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              tabIndex={0}
              className={cn(
                "h-2.5 w-2.5 shrink-0 cursor-help rounded-full",
                isLive ? "bg-status-ok" : "bg-status-bad",
              )}
            />
          </TooltipTrigger>
          <TooltipContent>
            {isLive
              ? "실행 중(live): 매매 엔진이 이 전략의 신호를 평가해 실제 주문을 냅니다."
              : "정지됨(stopped): 이 전략은 꺼져 있어 신호가 나도 주문하지 않습니다. '시작'을 눌러 활성화하세요."}
          </TooltipContent>
        </Tooltip>
        <div>
          <p className="text-sm font-medium">{strategy.name}</p>
          <p className="text-xs text-muted-foreground">
            {summarizeConfig(strategy.config)}
          </p>
        </div>
      </div>
      {/* 실행 중 = 가동 상태(초록 '중지'), 정지 = 미가동 상태(빨강 '시작').
          버튼 색은 현재 상태를 나타낸다 — 초록이면 운용 중, 빨강이면 꺼짐. */}
      <Button
        size="sm"
        variant={isLive ? "default" : "destructive"}
        onClick={() => toggle.mutate()}
        disabled={toggle.isPending}
        className={cn(isLive && "bg-status-ok text-white hover:bg-status-ok/90")}
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
 * 라이브 전략 러너 헬스 목록.
 * `GET /api/engine/strategies/health` 를 30초 주기로 폴링해(+ WS "alert" 수신 시 즉시 무효화),
 * 각 러너의 마지막 성공 틱 상대시각·연속 실패 횟수·healthy 배지를 보여준다.
 * 엔진 프로세스 전역 생존(상단 배지)과 달리, 전략 단위로 "조용한 실패"(외부 조회 지연 등)를 드러낸다.
 * MDD 킬스위치가 발동 중이면(전량 청산 후 쿨다운 재가동 대기) 별도 배지로 표시한다 —
 * 이전엔 발동 순간의 토스트/알림함 메시지 외엔 이 상태를 나중에 확인할 방법이 없었다.
 */
function StrategyHealthPanel({
  data,
  isLoading,
  isError,
}: {
  data?: { threshold: number; strategies: StrategyHealth[] };
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading) {
    return <p className="text-sm text-muted-foreground">헬스 상태 확인 중…</p>;
  }
  if (isError) {
    return (
      <p className="flex items-center gap-1 text-sm text-status-bad">
        <AlertCircle className="h-3.5 w-3.5" /> 헬스 상태를 불러오지 못했습니다.
      </p>
    );
  }
  const strategies = data?.strategies ?? [];
  if (strategies.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        실행 중(live)인 전략이 없습니다. 전략을 시작하면 여기에 헬스가 표시됩니다.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {strategies.map((s) => (
        <Card key={s.strategy_id} className="flex items-center justify-between px-4 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{s.name}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {s.reported ? (
                <>마지막 성공 틱: {formatRelativeTime(s.last_success_ts)}</>
              ) : (
                "아직 첫 틱 전(방금 시작)"
              )}
              {s.consecutive_failures > 0 && (
                <span className="ml-2 text-status-bad">
                  연속 실패 {s.consecutive_failures}회
                </span>
              )}
            </p>
            {s.last_error && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <p className="mt-0.5 cursor-help truncate text-xs text-status-bad/80">
                    최근 오류: {s.last_error}
                  </p>
                </TooltipTrigger>
                <TooltipContent>{s.last_error}</TooltipContent>
              </Tooltip>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {s.mdd_killed && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span tabIndex={0} className="cursor-help">
                    <Badge variant="warning">MDD 킬 발동중</Badge>
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  {s.mdd_kill_date
                    ? `${s.mdd_kill_date}에 고점 대비 낙폭이 임계를 넘어 전량 청산됐습니다. `
                    : "고점 대비 낙폭이 임계를 넘어 전량 청산됐습니다. "}
                  쿨다운(거래일 경과) 후 자동 재가동됩니다.
                </TooltipContent>
              </Tooltip>
            )}
            <Tooltip>
              <TooltipTrigger asChild>
                <span tabIndex={0} className="cursor-help">
                  <Badge variant={!s.reported ? "muted" : s.healthy ? "success" : "destructive"}>
                    {!s.reported ? "대기" : s.healthy ? "정상" : "이상"}
                  </Badge>
                </span>
              </TooltipTrigger>
              <TooltipContent>
                {!s.reported
                  ? "엔진이 이 전략을 시작했지만 아직 첫 틱을 처리하지 못했습니다."
                  : s.healthy
                    ? `연속 실패 ${s.consecutive_failures}회 (임계 ${data?.threshold} 미만) — 정상 처리 중입니다.`
                    : `연속 실패 ${s.consecutive_failures}회로 임계(${data?.threshold})를 넘었습니다. 외부 조회 지연·오류가 지속되고 있을 수 있습니다.`}
              </TooltipContent>
            </Tooltip>
          </div>
        </Card>
      ))}
    </div>
  );
}

/** 등급(GREEN/AMBER/RED/INSUFFICIENT) → Badge variant. */
function gradeBadgeVariant(
  grade: string,
): "success" | "warning" | "destructive" | "muted" {
  if (grade === "GREEN") return "success";
  if (grade === "AMBER") return "warning";
  if (grade === "RED") return "destructive";
  return "muted";
}

const GRADE_LABEL: Record<string, string> = {
  GREEN: "양호",
  AMBER: "주의",
  RED: "이탈",
  INSUFFICIENT: "표본 부족",
};

/**
 * 체결 정합 실측 카드(P2-3, D-2). 실거래 체결이 백테스트 슬리피지 가정(M1)·전체 체결가
 * 가정(M3)과 얼마나 어긋나는지 bp 단위로 보여준다. M2(시점 규약 표류)는 등급 없이 참고용
 * 진단 보조 지표로만 노출한다. 표본(min_sample) 미달이면 등급이 "표본 부족"으로 나오는 게
 * 정상 — 매매 이력이 쌓일수록(B-2 주간 배치가 자동 재점검) 신뢰도가 오른다. RED 등급이면
 * B-2 배치가 텔레그램으로도 알린다.
 */
function FillQualityPanel({
  data,
  isLoading,
  isError,
  strategyId,
}: {
  data?: FillQualityReport;
  isLoading: boolean;
  isError: boolean;
  /** 선택된 전략(전체 보기면 undefined) — 슬리피지 캘리브레이션 승인 대상. */
  strategyId?: number;
}) {
  if (isLoading) {
    return <p className="text-sm text-muted-foreground">체결 정합 조회 중…</p>;
  }
  if (isError) {
    return (
      <p className="flex items-center gap-1 text-sm text-status-bad">
        <AlertCircle className="h-3.5 w-3.5" /> 체결 정합 리포트를 불러오지 못했습니다.
      </p>
    );
  }
  if (!data || data.sample.n_orders === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        최근 90일 실거래 체결이 없습니다. 라이브 매매가 시작되면 여기에 표시됩니다.
      </p>
    );
  }

  const m1 = data.m1_exec.all;
  const m2 = data.m2_time.all;
  const m3 = data.m3_total.all;
  const drag = data.annualized_drag.drag_diff_pct_per_yr;

  return (
    <Card className="space-y-3 p-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <FillQualityMetric
          label="M1 실행 슬리피지"
          grade={data.grades.m1_exec}
          value={m1.mean !== null ? `${m1.mean.toFixed(1)}bp` : "-"}
          hint={`가정 ${data.assumptions.backtest_slip_bps}bp`}
        />
        <FillQualityMetric
          label="M2 시점 규약 표류"
          value={m2.mean !== null ? `${m2.mean.toFixed(1)}bp` : "-"}
          hint="참고용, 별도 등급 없음"
        />
        <FillQualityMetric
          label="M3 총 정합 괴리"
          grade={data.grades.m3_total}
          value={m3.mean !== null ? `${m3.mean.toFixed(1)}bp` : "-"}
          hint={drag !== null ? `연환산 드래그차 ${drag.toFixed(2)}%p/yr` : "회전율 미상"}
        />
        <div>
          <p className="text-xs text-muted-foreground">표본</p>
          <p className="mt-0.5 text-sm font-medium tabular-nums">
            {data.sample.n_orders}건 / 하한 {data.sample.min_sample}
          </p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">체크섬(1차 근사 잔차)</p>
          <p className="mt-0.5 text-sm font-medium tabular-nums">
            {data.checksum.mean_abs_residual_bp !== null
              ? `${data.checksum.mean_abs_residual_bp.toFixed(1)}bp`
              : "-"}
          </p>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        M1은 주문 결정가 대비 실제 체결가의 미끄러짐(백테스트 slippage_bps 가정 검증),
        M2는 라이브가 체결한 당일 종가와 백테스트가 가정하는 익일 종가 사이의 시장 이동(시점
        규약이 만드는 계통적 표류 — 진단 보조 지표로 별도 등급 없이 참고만 한다),
        M3은 실제 체결가와 백테스트 체결모형(익일 종가±슬리피지) 가정가의 총 괴리다.
        표본이 {data.sample.min_sample}건 미만인 방향은 &quot;표본 부족&quot;으로 판정을
        보류한다 — 과최적화 방어 통계와 같은 원칙(보일 때만 행동을 바꾼다).
      </p>
      {data.calibration && (
        <SlippageCalibrationBanner calibration={data.calibration} strategyId={strategyId} />
      )}
    </Card>
  );
}

/**
 * 슬리피지 캘리브레이션 승인 배너. 실측 중앙값 기반 제안(bp)을 보여주고, 사람이
 * 확인 후 승인하면 전략 config 의 slippage_bps 를 갱신한다(자동 반영 없음).
 * 전체 보기(strategyId undefined)에서는 반영 대상 전략이 모호하므로 승인 버튼을
 * 숨기고 "전략을 선택하라"는 안내만 보여준다.
 */
function SlippageCalibrationBanner({
  calibration,
  strategyId,
}: {
  calibration: CalibrationProposal;
  strategyId?: number;
}) {
  const qc = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);

  const apply = useMutation({
    mutationFn: () => {
      if (strategyId === undefined) {
        throw new Error("전략을 선택한 뒤 승인할 수 있습니다.");
      }
      return api.applySlippageCalibration(strategyId, calibration.proposed_bps);
    },
    onSuccess: (res) => {
      setNotice(
        `반영 완료: slippage_bps ${res.previous_bps}bp → ${res.applied_bps}bp (표본 ${res.sample_size}건)`,
      );
      qc.invalidateQueries({ queryKey: ["fill-quality"] });
      qc.invalidateQueries({ queryKey: ["strategies"] });
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setNotice("리포트가 갱신되어 제안이 달라졌습니다. 새로고침 후 다시 시도하세요.");
        qc.invalidateQueries({ queryKey: ["fill-quality"] });
        return;
      }
      setNotice(err instanceof Error ? err.message : "반영에 실패했습니다.");
    },
  });

  const handleApply = () => {
    if (strategyId === undefined) return;
    const ok = window.confirm(
      `전략의 slippage_bps 를 ${calibration.current_bps}bp → ${calibration.proposed_bps}bp 로 ` +
        `변경합니다(실측 중앙값 ${calibration.observed_median_bps.toFixed(1)}bp, 표본 ` +
        `${calibration.sample_size}건). 계속하시겠습니까?`,
    );
    if (!ok) return;
    setNotice(null);
    apply.mutate();
  };

  return (
    <div className="rounded-md border border-dashed border-amber-500/40 bg-amber-500/5 p-3 text-xs">
      <p className="font-medium text-foreground">
        슬리피지 캘리브레이션 제안: 현재 {calibration.current_bps}bp → 제안{" "}
        {calibration.proposed_bps}bp (표본 {calibration.sample_size}건)
        {calibration.clamped && " · 클램프 적용됨"}
      </p>
      <p className="mt-1 text-muted-foreground">{calibration.reason}</p>
      {strategyId === undefined ? (
        <p className="mt-2 text-muted-foreground">
          승인하려면 위 &quot;대상 전략&quot;에서 특정 전략을 선택하세요.
        </p>
      ) : (
        <div className="mt-2 flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={handleApply}
            disabled={apply.isPending}
          >
            {apply.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              "제안 적용"
            )}
          </Button>
        </div>
      )}
      {notice && <p className="mt-2 text-foreground">{notice}</p>}
    </div>
  );
}

function FillQualityMetric({
  label,
  grade,
  value,
  hint,
}: {
  label: string;
  /** 백엔드가 등급을 매기지 않는 보조 지표(M2 등)는 생략한다 — 뱃지 대신 hint로만 안내. */
  grade?: string;
  value: string;
  hint: string;
}) {
  return (
    <div>
      <div className="flex items-center gap-1.5">
        <p className="text-xs text-muted-foreground">{label}</p>
        {grade && (
          <Badge variant={gradeBadgeVariant(grade)} className="px-1.5 py-0 text-[10px]">
            {GRADE_LABEL[grade] ?? grade}
          </Badge>
        )}
      </div>
      <p className="mt-0.5 text-sm font-medium tabular-nums">{value}</p>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
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
