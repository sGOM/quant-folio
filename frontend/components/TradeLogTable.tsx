"use client";

import { useMemo, useState } from "react";
import { BacktestTrade } from "@/lib/api";
import { fmtPct, formatKRW, pctColor } from "@/lib/format";

// lib/format 공용 헬퍼에 이 컴포넌트의 표준 자릿수(2)만 입힌 별칭 — 포맷 로직 중복 금지.
const pct = (x: number | null | undefined) => fmtPct(x, 2);

/** 체결 로그 페이지 단위: 기본 최근 60건, "더 보기"로 60건씩 추가 노출. */
const TRADE_PAGE = 60;

/**
 * 백테스트 체결 로그 테이블. 노출 건수(더 보기/전체/접기)와 정렬(일자·종목코드,
 * 헤더 클릭 토글) 상태를 자체 관리한다.
 * @param trades 백테스트 체결 목록(시간 오름차순)
 * @param nameOf 종목코드 → "종목명(코드)" 변환(useSymbolNames)
 */
export function TradeLogTable({
  trades,
  nameOf,
}: {
  trades: BacktestTrade[];
  nameOf: (code: string) => string;
}) {
  const [limit, setLimit] = useState(TRADE_PAGE);
  // 정렬: 일자(기본, 최근 우선) 또는 종목코드.
  const [sort, setSort] = useState<{
    key: "time" | "code";
    dir: "asc" | "desc";
  }>({ key: "time", dir: "desc" });
  const total = trades.length;

  // 표시할 체결: 최근 limit 건을 창으로 잡고, 선택한 기준으로 정렬한다.
  // (limit=최근 몇 건을 볼지, sort=그 창 안의 정렬 순서 — 두 개념을 분리)
  const shown = useMemo(() => {
    const window = trades.slice(-limit);
    const d = sort.dir === "asc" ? 1 : -1;
    return [...window].sort((a, b) => {
      if (sort.key === "code") {
        const c = String(a.symbol).localeCompare(String(b.symbol));
        // 같은 종목이면 시간 오름차순으로 안정 정렬
        return c !== 0 ? c * d : String(a.t).localeCompare(String(b.t));
      }
      return String(a.t).localeCompare(String(b.t)) * d;
    });
  }, [trades, limit, sort]);

  // 헤더 클릭: 같은 열이면 방향 토글, 다른 열이면 기본 방향으로 전환
  // (일자=최근 우선 desc, 종목코드=오름차순 asc).
  function toggleSort(key: "time" | "code") {
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "time" ? "desc" : "asc" },
    );
  }
  const sortArrow = (key: "time" | "code") =>
    sort.key === key ? (sort.dir === "asc" ? " ↑" : " ↓") : "";

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="text-sm text-muted-foreground">
          체결 로그 (매수/매도 · 최근 {Math.min(limit, total)}건 / 전체 {total}건)
        </h2>
        <div className="flex shrink-0 gap-1">
          {limit < total && (
            <button
              type="button"
              onClick={() => setLimit((n) => n + TRADE_PAGE)}
              className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              더 보기 (+{Math.min(TRADE_PAGE, total - limit)}건)
            </button>
          )}
          {limit < total && (
            <button
              type="button"
              onClick={() => setLimit(total)}
              className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              전체 보기
            </button>
          )}
          {limit > TRADE_PAGE && (
            <button
              type="button"
              onClick={() => setLimit(TRADE_PAGE)}
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
                  onClick={() => toggleSort("time")}
                  className="font-normal transition-colors hover:text-foreground"
                  title="일자순 정렬"
                >
                  일자{sortArrow("time")}
                </button>
              </th>
              <th className="py-1 text-left font-normal">
                <button
                  type="button"
                  onClick={() => toggleSort("code")}
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
            {shown.map((tr, i) => (
              <tr key={i} className="border-b border-border/50">
                <td className="py-1">{tr.t.slice(0, 10)}</td>
                <td className="py-1">{nameOf(tr.symbol)}</td>
                <td className="py-1 text-center">
                  <span
                    className={
                      tr.side === "buy"
                        ? "text-profit"
                        : tr.reason === "regime_exit" || tr.reason === "mdd_kill"
                          ? "text-amber-500"
                          : "text-loss"
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
                <td className="py-1 text-right">{formatKRW(tr.amount)}</td>
                <td className="py-1 text-right">
                  {tr.price == null ? "-" : formatKRW(tr.price, false)}
                </td>
                <td className={`py-1 text-right ${pctColor(tr.position_return)}`}>
                  {tr.position_return === null || tr.position_return === undefined
                    ? "-"
                    : pct(tr.position_return)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
