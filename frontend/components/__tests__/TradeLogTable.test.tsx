import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { TradeLogTable } from "@/components/TradeLogTable";
import { BacktestTrade } from "@/lib/api";

/** 테스트용 최소 체결 로그 항목을 만든다(t=일자, 나머지는 기본값에서 덮어쓴다). */
function trade(over: Partial<BacktestTrade> & { t: string }): BacktestTrade {
  return {
    symbol: "005930",
    side: "buy",
    amount: 1_000_000,
    price: 70_000,
    port_return: 0,
    position_return: null,
    reason: "rebalance",
    ...over,
  };
}

const nameOf = (code: string) => `삼성전자(${code})`;

describe("TradeLogTable", () => {
  it("빈 배열이면 0건으로 표시하고 행이 없다", () => {
    render(<TradeLogTable trades={[]} nameOf={nameOf} />);
    expect(screen.getByText(/전체 0건/)).toBeInTheDocument();
    expect(screen.queryAllByRole("row")).toHaveLength(1); // 헤더행만
  });

  it("포지션손익이 양수면 text-profit, 음수면 text-loss 클래스를 적용한다", () => {
    const trades = [
      trade({ t: "2024-01-01T00:00:00Z", side: "sell", position_return: 0.05 }),
      trade({ t: "2024-01-02T00:00:00Z", side: "sell", position_return: -0.03 }),
      trade({ t: "2024-01-03T00:00:00Z", side: "buy", position_return: null }),
    ];
    render(<TradeLogTable trades={trades} nameOf={nameOf} />);

    const rows = screen.getAllByRole("row").slice(1); // 헤더 제외
    // 기본 정렬은 일자 desc → 01-03, 01-02, 01-01 순
    const cellsOf = (row: HTMLElement) => within(row).getAllByRole("cell");

    const row0 = cellsOf(rows[0]);
    expect(row0[5].textContent).toBe("-"); // buy → position_return null
    expect(row0[5].className).toContain("text-muted-foreground");

    const row1 = cellsOf(rows[1]);
    expect(row1[5].textContent).toBe("-3.00%");
    expect(row1[5].className).toContain("text-loss");

    const row2 = cellsOf(rows[2]);
    expect(row2[5].textContent).toBe("+5.00%");
    expect(row2[5].className).toContain("text-profit");
  });

  it("매수는 매수 배지(text-profit), 일반 매도는 매도 배지(text-loss)로 표시한다", () => {
    const trades = [
      trade({ t: "2024-01-01T00:00:00Z", side: "buy" }),
      trade({ t: "2024-01-02T00:00:00Z", side: "sell", reason: "rebalance" }),
    ];
    render(<TradeLogTable trades={trades} nameOf={nameOf} />);
    expect(screen.getByText("매수").className).toContain("text-profit");
    expect(screen.getByText("매도").className).toContain("text-loss");
  });

  it("레짐 청산·MDD 킬스위치 사유는 경고색(amber)과 전용 라벨로 표시한다", () => {
    const trades = [
      trade({ t: "2024-01-01T00:00:00Z", side: "sell", reason: "regime_exit" }),
      trade({ t: "2024-01-02T00:00:00Z", side: "sell", reason: "mdd_kill" }),
    ];
    render(<TradeLogTable trades={trades} nameOf={nameOf} />);
    expect(screen.getByText("청산").className).toContain("text-amber-500");
    expect(screen.getByText("킬스위치").className).toContain("text-amber-500");
  });

  it("nameOf 로 종목명을 렌더링한다", () => {
    render(
      <TradeLogTable trades={[trade({ t: "2024-01-01T00:00:00Z" })]} nameOf={nameOf} />,
    );
    expect(screen.getByText("삼성전자(005930)")).toBeInTheDocument();
  });

  it("TRADE_PAGE(60건) 초과 시 '더 보기' 버튼이 나타나고 클릭하면 노출 건수가 늘어난다", () => {
    const trades = Array.from({ length: 70 }, (_, i) =>
      trade({ t: `2024-01-${String((i % 28) + 1).padStart(2, "0")}T00:00:00Z` }),
    );
    render(<TradeLogTable trades={trades} nameOf={nameOf} />);

    expect(screen.getByText(/최근 60건 \/ 전체 70건/)).toBeInTheDocument();
    const more = screen.getByRole("button", { name: /더 보기/ });
    fireEvent.click(more);
    expect(screen.getByText(/최근 70건 \/ 전체 70건/)).toBeInTheDocument();
  });

  it("종목 헤더 클릭 시 종목코드 기준 정렬로 전환된다", () => {
    const trades = [
      trade({ t: "2024-01-01T00:00:00Z", symbol: "000660" }),
      trade({ t: "2024-01-02T00:00:00Z", symbol: "005930" }),
    ];
    render(<TradeLogTable trades={trades} nameOf={(c) => c} />);

    const codeHeader = screen.getByRole("button", { name: /종목/ });
    fireEvent.click(codeHeader);

    const rows = screen.getAllByRole("row").slice(1);
    const firstRowCells = within(rows[0]).getAllByRole("cell");
    expect(firstRowCells[1].textContent).toBe("000660"); // 오름차순 첫 항목
  });
});
