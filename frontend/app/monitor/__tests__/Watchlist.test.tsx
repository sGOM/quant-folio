import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Watchlist } from "@/app/monitor/Watchlist";
import { api } from "@/lib/api";

/** 테스트마다 격리된 QueryClient 로 감싼 래퍼(캐시가 테스트 간 새지 않도록). */
function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe("Watchlist", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("localStorage.setItem 이 예외를 던져도(quota 초과 등) 종목 추가가 크래시하지 않는다", () => {
    vi.spyOn(api, "quote").mockResolvedValue({
      symbol: "005930",
      price: 100,
      change: 0,
      change_rate: 0,
      volume: 0,
      high: 0,
      low: 0,
      open: 0,
      currency: "KRW",
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });

    render(<Watchlist tossQuote={false} />, { wrapper: makeWrapper() });

    const input = screen.getByPlaceholderText(/국내 종목코드/);
    fireEvent.change(input, { target: { value: "005930" } });
    fireEvent.submit(input.closest("form")!);

    // setItem 이 던져도 종목은 화면에 정상 반영된다(크래시로 페이지가 날아가지 않는다).
    expect(screen.getByText("005930")).toBeInTheDocument();
  });
});
