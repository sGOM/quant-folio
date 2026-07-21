import { describe, expect, it, vi, afterEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Watchlist } from "@/app/monitor/page";

// QuoteRow가 마운트 즉시 호출하므로 최소 응답으로 고정해 테스트를 단순화한다.
vi.mock("@/lib/api", () => ({
  api: {
    quote: vi.fn().mockResolvedValue({
      price: 70_000,
      currency: "KRW",
      change: 0,
      change_rate: 0,
    }),
  },
  ApiError: class ApiError extends Error {},
}));

function renderWatchlist() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <Watchlist tossQuote={false} />
    </QueryClientProvider>,
  );
}

describe("Watchlist localStorage 영속", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("localStorage.setItem 이 예외를 던져도 페이지가 크래시하지 않고 종목이 화면에 반영된다", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("QuotaExceededError");
    });

    renderWatchlist();

    const input = screen.getByPlaceholderText(/국내 종목코드/);
    fireEvent.change(input, { target: { value: "005930" } });
    fireEvent.click(screen.getByRole("button", { name: "종목 추가" }));

    // 저장은 실패해도(throw) 컴포넌트 상태 갱신·렌더링은 정상 유지되어야 한다.
    expect(screen.getByText("005930")).toBeInTheDocument();
  });

  it("정상 상황에서는 추가한 종목을 localStorage 에 저장한다", () => {
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");

    renderWatchlist();

    const input = screen.getByPlaceholderText(/국내 종목코드/);
    fireEvent.change(input, { target: { value: "005930" } });
    fireEvent.click(screen.getByRole("button", { name: "종목 추가" }));

    expect(setItemSpy).toHaveBeenCalledWith(
      "watchlist:quotes",
      JSON.stringify(["005930"]),
    );
  });
});
