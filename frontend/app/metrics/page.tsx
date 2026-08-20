"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  Info,
  HelpCircle,
  ChevronDown,
  ChevronUp,
  ChevronsUpDown,
} from "lucide-react";
import {
  api,
  type MetricMarket,
  type SectorMetric,
  type StockMetric,
  type PanicMarket,
  type PanicLevel,
  type PanicSignal,
} from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { fmtPct, fmtNum, fmtAmt, fmtKRW, formatNumber, pctColor } from "@/lib/format";

/** 종합점수(score) 계산 방식 설명 — 헤더 툴팁·안내에 공용으로 사용. */
const SCORE_HINT =
  "종합점수 = 필터를 통과한 후보 종목군 안에서의 상대 우위를 표준화(z-score)해 합산한 값입니다. " +
  "모멘텀 40% + 밸류 30% + 저변동성 30% 가중 평균이며, 각 지표는 1·99% 윈저라이징 후 z-score로 변환합니다" +
  "(PER·PBR·변동성은 낮을수록 가점). 0 이상이면 평균 이상, 값이 클수록 상대적으로 우수합니다. 절대 수익률이 아닙니다.";

// ─────────────────────────── 포맷 헬퍼 ───────────────────────────

/** 기준일 문자열을 표시용으로 포맷. */
function fmtDate(s: string): string {
  if (!s) return "";
  return s.slice(0, 10);
}

// ─────────────────────────── 정렬(클라이언트) 유틸 ───────────────────────────
// 서버는 전체 스냅샷을 반환하므로, 정렬은 컬럼 머리글 클릭으로 클라이언트에서
// 처리한다. 같은 컬럼을 다시 클릭하면 desc ↔ asc 를 토글하고, null 값은 방향과
// 무관하게 항상 아래로 보낸다.

type SortDir = "asc" | "desc";
interface SortState<K extends string> {
  key: K;
  dir: SortDir;
}

/** 컬럼 클릭 정렬 상태 훅. 최초 클릭은 desc, 재클릭 시 asc 로 토글. */
function useTableSort<K extends string>(initialKey: K) {
  const [sort, setSort] = useState<SortState<K>>({
    key: initialKey,
    dir: "desc",
  });
  const onSort = (key: K) =>
    setSort((p) =>
      p.key === key
        ? { key, dir: p.dir === "desc" ? "asc" : "desc" }
        : { key, dir: "desc" },
    );
  return { sort, onSort };
}

/** rows 를 key/dir 로 정렬한 새 배열을 반환한다(null 은 항상 하단). */
function sortRows<T, K extends keyof T & string>(
  rows: T[],
  key: K,
  dir: SortDir,
): T[] {
  return [...rows].sort((a, b) => {
    const av = a[key] as unknown;
    const bv = b[key] as unknown;
    const aN = av == null;
    const bN = bv == null;
    if (aN && bN) return 0;
    if (aN) return 1; // null 하단
    if (bN) return -1;
    let cmp: number;
    if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
    else cmp = String(av).localeCompare(String(bv), "ko");
    return dir === "asc" ? cmp : -cmp;
  });
}

/** 머리글 라벨 옆에 붙는 설명 툴팁 아이콘. 클릭 시 정렬로 이벤트가 전파되지 않는다. */
function HintIcon({ text }: { text: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label="설명"
          onClick={(e) => e.stopPropagation()}
          className="inline-flex text-muted-foreground/70 transition-colors hover:text-foreground"
        >
          <HelpCircle className="h-3.5 w-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  );
}

/** 클릭으로 정렬되는 표 머리글 셀. 활성 컬럼에 방향 화살표를 표시한다. */
function SortHeader<K extends string>({
  label,
  colKey,
  sort,
  onSort,
  align = "right",
  sticky = false,
  hint,
}: {
  label: string;
  colKey: K;
  sort: SortState<K>;
  onSort: (k: K) => void;
  align?: "left" | "right";
  sticky?: boolean;
  hint?: string;
}) {
  const active = sort.key === colKey;
  return (
    <th
      onClick={() => onSort(colKey)}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      className={cn(
        "cursor-pointer select-none px-3 py-2.5 font-medium transition-colors hover:text-foreground",
        align === "right" ? "text-right" : "text-left",
        sticky && "sticky left-0 z-10 bg-muted/50",
        active && "text-foreground",
      )}
    >
      <span
        className={cn(
          "inline-flex items-center gap-1",
          align === "right" && "flex-row-reverse",
        )}
      >
        <span className="inline-flex items-center gap-1">
          {label}
          {hint && <HintIcon text={hint} />}
        </span>
        {active ? (
          sort.dir === "desc" ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronUp className="h-3 w-3" />
          )
        ) : (
          <ChevronsUpDown className="h-3 w-3 opacity-30" />
        )}
      </span>
    </th>
  );
}

// ─────────────────────────── 공통 SELECT 스타일 ───────────────────────────
// bg-transparent 대신 bg-input 을 명시해 네이티브 select 가 OS 기본(흰 배경)
// 콤보박스로 렌더되는 것을 방지하고, text-foreground 로 글씨 대비를 보장한다.
const SELECT_CLS =
  "h-8 rounded-md border border-input bg-input text-foreground px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring";

// ─────────────────────────── 페이지 진입점 ───────────────────────────

export default function MetricsPage() {
  return (
    <RequireAuth>
      <MetricsContent />
    </RequireAuth>
  );
}

function MetricsContent() {
  return (
    <TooltipProvider delayDuration={150}>
      <Nav />
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
        <div className="mb-5">
          <h1 className="text-2xl font-semibold tracking-tight">시장 지표</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            오늘 기준 KRX 섹터·종목 스냅샷입니다. 과거 백테스트와 다르며,
            모멘텀·유동성 지표가 신뢰도 높고 밸류·퀄리티는 참고 지표입니다.
          </p>
        </div>

        <Tabs defaultValue="sectors">
          <TabsList className="mb-6">
            <TabsTrigger value="sectors">섹터 지표</TabsTrigger>
            <TabsTrigger value="stocks">종목 지표</TabsTrigger>
            <TabsTrigger value="panic">패닉셀</TabsTrigger>
          </TabsList>

          <TabsContent value="sectors">
            <SectorsTab />
          </TabsContent>

          <TabsContent value="stocks">
            <StocksTab />
          </TabsContent>

          <TabsContent value="panic">
            <PanicTab />
          </TabsContent>
        </Tabs>
      </main>
    </TooltipProvider>
  );
}

// ─────────────────────────── 섹터 탭 ───────────────────────────

/** 섹터 표에서 정렬 가능한 컬럼 키(SectorMetric 필드). */
type SectorSortKey = keyof SectorMetric & string;

function SectorsTab() {
  const [market, setMarket] = useState<MetricMarket>("ALL");
  const { sort, onSort } = useTableSort<SectorSortKey>("rs_126");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["metrics", "sectors", market],
    queryFn: () => api.getSectors(market),
    staleTime: 5 * 60 * 1000, // 5분
  });

  const rows = useMemo(
    () => (data ? sortRows(data.items, sort.key, sort.dir) : []),
    [data, sort.key, sort.dir],
  );

  return (
    <div>
      {/* 필터 바 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-muted-foreground">시장</span>
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as MetricMarket)}
            className={SELECT_CLS}
          >
            <option value="ALL">전체</option>
            <option value="KOSPI">KOSPI</option>
            <option value="KOSDAQ">KOSDAQ</option>
          </select>
        </label>
        {data && (
          <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
            <Info className="h-3.5 w-3.5" />
            기준일: {fmtDate(data.as_of)}
          </span>
        )}
      </div>

      {/* 안내 배너 */}
      <InfoBanner text="모멘텀(RS·6M·3M)·거래대금추세가 핵심 선별 지표입니다. 컬럼 머리글을 클릭하면 해당 지표로 정렬됩니다(다시 클릭하면 오름/내림 전환)." />

      {/* 로딩/에러/데이터 */}
      {isLoading && <TableSkeleton rows={8} />}
      {isError && <ErrorState message={(error as Error).message} />}
      {data && rows.length === 0 && (
        <EmptyState message="조건에 맞는 섹터 데이터가 없습니다." />
      )}
      {data && rows.length > 0 && (
        <SectorTable items={rows} sort={sort} onSort={onSort} />
      )}
    </div>
  );
}

function SectorTable({
  items,
  sort,
  onSort,
}: {
  items: SectorMetric[];
  sort: SortState<SectorSortKey>;
  onSort: (k: SectorSortKey) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[820px] text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            <SortHeader<SectorSortKey>
              label="섹터"
              colKey="name"
              sort={sort}
              onSort={onSort}
              align="left"
              sticky
            />
            <SortHeader<SectorSortKey>
              label="시장"
              colKey="market"
              sort={sort}
              onSort={onSort}
              align="left"
            />
            {/* 모멘텀 그룹 */}
            <SortHeader<SectorSortKey> label="3M 모멘텀" colKey="mom_3m" sort={sort} onSort={onSort} />
            <SortHeader<SectorSortKey> label="6M 모멘텀" colKey="mom_6m" sort={sort} onSort={onSort} />
            <SortHeader<SectorSortKey> label="1M 모멘텀" colKey="mom_1m" sort={sort} onSort={onSort} />
            {/* 강도·추세 */}
            <SortHeader<SectorSortKey> label="RS(126일)" colKey="rs_126" sort={sort} onSort={onSort} />
            <SortHeader<SectorSortKey> label="거래대금추세" colKey="value_trend" sort={sort} onSort={onSort} />
            {/* 리스크 */}
            <SortHeader<SectorSortKey> label="변동성(연)" colKey="vol_ann" sort={sort} onSort={onSort} />
            <SortHeader<SectorSortKey> label="위험조정모멘텀" colKey="risk_adj_mom" sort={sort} onSort={onSort} />
            <SortHeader<SectorSortKey> label="MDD(252일)" colKey="mdd_252" sort={sort} onSort={onSort} />
            {/* 가격 위치 */}
            <SortHeader<SectorSortKey> label="52주고가비" colKey="high_52w_ratio" sort={sort} onSort={onSort} />
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <SectorRow key={`${row.market}-${row.name}`} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SectorRow({ row }: { row: SectorMetric }) {
  return (
    <tr className="border-t border-border/60 transition-colors hover:bg-accent/30">
      <td className="sticky left-0 bg-background px-3 py-2 font-medium hover:bg-accent/30">
        {row.name}
      </td>
      <td className="px-3 py-2">
        <MarketBadge market={row.market} />
      </td>
      {/* 모멘텀 */}
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mom_3m))}>
        {fmtPct(row.mom_3m)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mom_6m))}>
        {fmtPct(row.mom_6m)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mom_1m))}>
        {fmtPct(row.mom_1m)}
      </td>
      {/* 강도·추세 */}
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.rs_126))}>
        {fmtNum(row.rs_126, 2)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.value_trend))}>
        {fmtNum(row.value_trend, 2)}
      </td>
      {/* 리스크 */}
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtPct(row.vol_ann)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.risk_adj_mom))}>
        {fmtNum(row.risk_adj_mom, 2)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mdd_252))}>
        {fmtPct(row.mdd_252)}
      </td>
      {/* 가격 위치 */}
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.high_52w_ratio))}>
        {fmtPct(row.high_52w_ratio)}
      </td>
    </tr>
  );
}

// ─────────────────────────── 종목 탭 ───────────────────────────

/** 종목 표에서 정렬 가능한 컬럼 키(StockMetric 필드). */
type StockSortKey = keyof StockMetric & string;

const LIMIT_OPTIONS = [30, 50, 100, 200];

function StocksTab() {
  const [market, setMarket] = useState<MetricMarket>("ALL");
  const [limit, setLimit] = useState(50);
  const { sort, onSort } = useTableSort<StockSortKey>("score");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["metrics", "stocks", market, limit],
    queryFn: () => api.getStocks({ market, limit }),
    staleTime: 5 * 60 * 1000,
  });

  const rows = useMemo(
    () => (data ? sortRows(data.items, sort.key, sort.dir) : []),
    [data, sort.key, sort.dir],
  );

  return (
    <div>
      {/* 필터 바 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-muted-foreground">시장</span>
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as MetricMarket)}
            className={SELECT_CLS}
          >
            <option value="ALL">전체</option>
            <option value="KOSPI">KOSPI</option>
            <option value="KOSDAQ">KOSDAQ</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-muted-foreground">표시</span>
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className={SELECT_CLS}
          >
            {LIMIT_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}개
              </option>
            ))}
          </select>
        </label>
        {data && (
          <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
            <Info className="h-3.5 w-3.5" />
            기준일: {fmtDate(data.as_of)} · 종합점수 상위 {formatNumber(data.count)}종목
          </span>
        )}
      </div>

      {/* 안내 배너 */}
      <InfoBanner text="종합점수(모멘텀·밸류·저변동성 가중합) 상위 종목입니다. 컬럼 머리글을 클릭하면 이 목록을 해당 지표로 재정렬합니다(다시 클릭하면 오름/내림 전환)." />

      {/* 로딩/에러/데이터 */}
      {isLoading && <TableSkeleton rows={10} />}
      {isError && <ErrorState message={(error as Error).message} />}
      {data && rows.length === 0 && (
        <EmptyState message="조건에 맞는 종목 데이터가 없습니다." />
      )}
      {data && rows.length > 0 && (
        <StocksTable items={rows} sort={sort} onSort={onSort} />
      )}
    </div>
  );
}

function StocksTable({
  items,
  sort,
  onSort,
}: {
  items: StockMetric[];
  sort: SortState<StockSortKey>;
  onSort: (k: StockSortKey) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[1200px] text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            <SortHeader<StockSortKey>
              label="종목"
              colKey="name"
              sort={sort}
              onSort={onSort}
              align="left"
              sticky
            />
            <SortHeader<StockSortKey> label="시장" colKey="market" sort={sort} onSort={onSort} align="left" />
            {/* 가격 */}
            <SortHeader<StockSortKey> label="현재가" colKey="price" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="등락률" colKey="change_rate" sort={sort} onSort={onSort} />
            {/* 규모 */}
            <SortHeader<StockSortKey> label="시총" colKey="market_cap" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="거래대금(20일)" colKey="avg_value_20" sort={sort} onSort={onSort} />
            {/* 밸류 */}
            <SortHeader<StockSortKey> label="PER" colKey="per" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="PBR" colKey="pbr" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="배당(%)" colKey="div" sort={sort} onSort={onSort} />
            {/* 모멘텀 */}
            <SortHeader<StockSortKey> label="3M" colKey="mom_3m" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="6M" colKey="mom_6m" sort={sort} onSort={onSort} />
            {/* 기술적 */}
            <SortHeader<StockSortKey> label="RSI14" colKey="rsi14" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="변동성" colKey="vol_ann" sort={sort} onSort={onSort} />
            <SortHeader<StockSortKey> label="MDD" colKey="mdd_252" sort={sort} onSort={onSort} />
            {/* 상태(정렬 불가) */}
            <th className="px-3 py-2.5 text-center font-medium">상태</th>
            {/* 점수 */}
            <SortHeader<StockSortKey>
              label="종합점수"
              colKey="score"
              sort={sort}
              onSort={onSort}
              hint={SCORE_HINT}
            />
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <StockRow key={row.code} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StockRow({ row }: { row: StockMetric }) {
  return (
    <tr className="border-t border-border/60 transition-colors hover:bg-accent/30">
      <td className="sticky left-0 bg-background px-3 py-2 hover:bg-accent/30">
        <div className="font-medium leading-tight">{row.name}</div>
        <div className="font-mono text-[11px] text-muted-foreground">{row.code}</div>
      </td>
      <td className="px-3 py-2">
        <MarketBadge market={row.market} />
      </td>
      {/* 가격 */}
      <td className="px-3 py-2 text-right tabular-nums">
        {fmtKRW(row.price, false)}
      </td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          pctColor(row.change_rate),
        )}
      >
        {row.change_rate == null
          ? "-"
          : `${row.change_rate > 0 ? "▲" : row.change_rate < 0 ? "▼" : "─"} ${fmtPct(row.change_rate)}`}
      </td>
      {/* 규모 */}
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtAmt(row.market_cap)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtAmt(row.avg_value_20)}
      </td>
      {/* 밸류 */}
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtNum(row.per, 1)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtNum(row.pbr, 2)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {row.div == null ? "-" : `${row.div.toFixed(2)}%`}
      </td>
      {/* 모멘텀 */}
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mom_3m))}>
        {fmtPct(row.mom_3m)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mom_6m))}>
        {fmtPct(row.mom_6m)}
      </td>
      {/* 기술적 */}
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtNum(row.rsi14, 1)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtPct(row.vol_ann)}
      </td>
      <td className={cn("px-3 py-2 text-right tabular-nums", pctColor(row.mdd_252))}>
        {fmtPct(row.mdd_252)}
      </td>
      {/* 상태 배지 */}
      <td className="px-3 py-2">
        <div className="flex justify-center gap-1">
          {row.trend_aligned && (
            <Badge variant="success" className="text-[10px]">
              정배열
            </Badge>
          )}
          {row.above_sma200 && (
            <Badge variant="warning" className="text-[10px]">
              200선↑
            </Badge>
          )}
          {!row.trend_aligned && !row.above_sma200 && (
            <span className="text-xs text-muted-foreground/50">-</span>
          )}
        </div>
      </td>
      {/* 종합점수 */}
      <td className="px-3 py-2 text-right">
        <ScoreCell row={row} />
      </td>
    </tr>
  );
}

/** 서브스코어 한 줄(라벨 + z-score 값). null 은 "-"로 표시. */
function fmtSubscore(v: number | null): string {
  return v == null ? "-" : v.toFixed(2);
}

/**
 * 종합점수 강조 셀. score 는 z-score 합성값(대략 -2~+2)으로, 0 이상이 평균 이상이다.
 * 호버(포커스)하면 가치·모멘텀·저변동성 서브스코어 분해를 툴팁으로 보여준다(§14).
 */
function ScoreCell({ row }: { row: StockMetric }) {
  const { score, score_value, score_momentum, score_lowvol } = row;
  if (score == null) return <span className="text-muted-foreground">-</span>;
  const color =
    score >= 0.5
      ? "text-profit font-semibold"
      : score >= 0
        ? "text-foreground font-medium"
        : "text-muted-foreground";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className={cn("cursor-help tabular-nums underline decoration-dotted decoration-muted-foreground/40", color)}
        >
          {score.toFixed(2)}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5 text-xs">
          <p className="font-medium text-foreground">서브스코어 분해(z-score)</p>
          <p>모멘텀 40%: {fmtSubscore(score_momentum)}</p>
          <p>밸류 30%: {fmtSubscore(score_value)}</p>
          <p>저변동성 30%: {fmtSubscore(score_lowvol)}</p>
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

// ─────────────────────────── 패닉셀 탭 ───────────────────────────

/** 경보 단계별 표시 메타(라벨·배지 색·설명). */
const PANIC_LEVEL_META: Record<
  PanicLevel,
  { label: string; badge: "muted" | "warning" | "destructive"; ring: string; desc: string }
> = {
  normal: {
    label: "정상",
    badge: "muted",
    ring: "border-border",
    desc: "통상적 등락 범위입니다. 특이 대응이 필요하지 않습니다.",
  },
  caution: {
    label: "주의",
    badge: "warning",
    ring: "border-amber-500/40",
    desc: "매도압력이 커지고 브레드스가 약화되고 있습니다. 조정 초입일 수 있어 추격매수·신규 레버리지를 자제하세요.",
  },
  warning: {
    label: "경계",
    badge: "warning",
    ring: "border-amber-500/60",
    desc: "강한 하락이나 투매(광범위+거래폭증)로 확증되진 않았습니다. 리스크 축소를 검토하되 반등 가능성도 열려 있습니다.",
  },
  panic: {
    label: "패닉·투매",
    badge: "destructive",
    ring: "border-destructive/60",
    desc: "무차별 투매·자본항복 국면입니다. 역설적으로 중장기 저점 부근일 수 있어, 투매 동참은 최악의 타이밍이 될 수 있습니다. 분할 대응·현금비중 관리 관점으로 보세요.",
  },
};

/** 시그널 원시값을 key에 맞는 단위로 포맷한다. */
function fmtSignalValue(sig: PanicSignal): string {
  if (sig.value == null) return "-";
  switch (sig.key) {
    case "r1":
    case "r5":
    case "dd60":
    case "bdr":
    case "cr5":
    case "cr10":
      return fmtPct(sig.value);
    case "vr":
      return `${sig.value.toFixed(2)}×`;
    default: // z, clv
      return fmtNum(sig.value, 2);
  }
}

function PanicTab() {
  const [market, setMarket] = useState<MetricMarket>("ALL");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["metrics", "panic", market],
    queryFn: () => api.getPanic(market),
    staleTime: 5 * 60 * 1000,
  });

  return (
    <div>
      {/* 필터 바 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-sm">
          <span className="text-muted-foreground">시장</span>
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value as MetricMarket)}
            className={SELECT_CLS}
          >
            <option value="ALL">전체</option>
            <option value="KOSPI">KOSPI</option>
            <option value="KOSDAQ">KOSDAQ</option>
          </select>
        </label>
        {data && (
          <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
            <Info className="h-3.5 w-3.5" />
            기준일: {fmtDate(data.as_of)} · 종가 확정 기준(장중 실시간 아님)
          </span>
        )}
      </div>

      {/* 해석 주의 배너 — 매매 신호가 아님을 명시 */}
      <div className="mb-4 flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-muted-foreground">
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
        <span>
          패닉셀 지표는 시장 공포 국면을 알리는 <b>컨텍스트</b>일 뿐 매매 신호가 아닙니다. 자본항복은
          통계적으로 바닥 근처에서 발생하는 경우가 많아, 투매에 동참하는 것이 최악의 타이밍이 될 수
          있습니다. 일봉 종가 기반이라 장중 급락 후 회복(V자)은 감지하지 못합니다.
        </span>
      </div>

      {/* 방법론상 알려진 한계 고지(백엔드 caveats 배선 전에는 미노출) */}
      {data?.caveats && data.caveats.length > 0 && (
        <div className="mb-4 flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <ul className="list-disc space-y-0.5 pl-4">
            {data.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {/* 로딩/에러/데이터 */}
      {isLoading && <TableSkeleton rows={2} />}
      {isError && <ErrorState message={(error as Error).message} />}
      {data && data.items.length === 0 && (
        <EmptyState message="패닉셀 지표 데이터가 없습니다." />
      )}
      {data && data.items.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          {data.items.map((item) => (
            <PanicCard key={item.market} item={item} />
          ))}
        </div>
      )}
      {/* 계산 불가 시장 명시 — "정상"과 "계산 불가"를 구분 */}
      {data && data.unavailable.length > 0 && (
        <div className="mt-4 flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {data.unavailable.join(", ")} 시장은 데이터 조회 실패로 계산하지 못했습니다(이상 없음이 아님).
          </span>
        </div>
      )}
    </div>
  );
}

/** 시장 1개의 패닉 판정 카드. */
function PanicCard({ item }: { item: PanicMarket }) {
  const meta = PANIC_LEVEL_META[item.level] ?? PANIC_LEVEL_META.normal;
  return (
    <div className={cn("rounded-lg border-2 bg-card p-4", meta.ring)}>
      {/* 헤더: 시장 + 라벨 + 점수 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <MarketBadge market={item.market} />
          <Badge variant={meta.badge} className="text-[11px]">
            {meta.label}
          </Badge>
          {item.hard_trigger && (
            <Badge variant="destructive" className="text-[10px]">
              하드트리거
            </Badge>
          )}
        </div>
        <div className="text-right tabular-nums">
          <span className="text-2xl font-semibold">{Math.round(item.score)}</span>
          <span className="text-sm text-muted-foreground"> / 100</span>
        </div>
      </div>

      {/* 점수 게이지 */}
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            "h-full rounded-full transition-all",
            item.level === "panic"
              ? "bg-destructive"
              : item.level === "normal"
                ? "bg-muted-foreground/40"
                : "bg-amber-500",
          )}
          style={{ width: `${Math.max(0, Math.min(100, item.score))}%` }}
        />
      </div>

      {/* 단계 설명 */}
      <p className="mt-3 text-xs leading-relaxed text-muted-foreground">{meta.desc}</p>

      {/* 축 요약 */}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>가격축 {item.price_sub == null ? "-" : Math.round(item.price_sub)}</span>
        <span>브레드스축 {item.breadth_sub == null ? "-" : Math.round(item.breadth_sub)}</span>
        <span>게이팅 {item.gated ? "충족" : "미충족"}</span>
        <span>60일고점대비 {fmtPct(item.dd60)}</span>
        <span>유효종목 {formatNumber(item.universe)}</span>
      </div>

      {/* 시그널 분해 */}
      <div className="mt-3 space-y-1.5 border-t border-border/60 pt-3">
        {item.signals
          .filter((s) => s.weight > 0)
          .map((sig) => (
            <PanicSignalRow key={sig.key} sig={sig} />
          ))}
      </div>
    </div>
  );
}

/** 시그널 1행: 라벨·원시값·서브스코어 미니바. */
function PanicSignalRow({ sig }: { sig: PanicSignal }) {
  const sub = sig.subscore;
  const barColor =
    sub == null
      ? "bg-muted-foreground/30"
      : sub >= 100
        ? "bg-destructive"
        : sub >= 50
          ? "bg-amber-500"
          : "bg-muted-foreground/50";
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-32 shrink-0 text-muted-foreground">{sig.label}</span>
      <span className="w-16 shrink-0 text-right tabular-nums">{fmtSignalValue(sig)}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full", barColor)}
          style={{ width: `${sub == null ? 0 : Math.max(0, Math.min(100, sub))}%` }}
        />
      </div>
    </div>
  );
}

// ─────────────────────────── 공용 서브 컴포넌트 ───────────────────────────

/** 표 로딩 스켈레톤. */
function TableSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full rounded-md" />
      ))}
    </div>
  );
}

/** 시장 구분 배지. */
function MarketBadge({ market }: { market: string }) {
  return (
    <Badge
      variant={market === "KOSPI" ? "secondary" : market === "KOSDAQ" ? "muted" : "outline"}
      className="text-[10px]"
    >
      {market}
    </Badge>
  );
}

/** 정보 배너. */
function InfoBanner({ text }: { text: string }) {
  return (
    <div className="mb-4 flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{text}</span>
    </div>
  );
}

/** 에러 상태. */
function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 py-12 text-center">
      <AlertCircle className="h-8 w-8 text-destructive" />
      <p className="text-sm font-medium text-destructive">데이터를 불러오지 못했습니다</p>
      <p className="text-xs text-muted-foreground">{message}</p>
    </div>
  );
}

/** 빈 상태. */
function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed py-12 text-center">
      <p className="text-sm font-medium text-muted-foreground">{message}</p>
    </div>
  );
}
