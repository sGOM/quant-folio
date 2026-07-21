import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { SymbolSearch } from "@/components/SymbolSearch";
import { api, SymbolHit } from "@/lib/api";

/** 테스트마다 격리된 QueryClient 로 감싼 래퍼(캐시가 테스트 간 새지 않도록). */
function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const SAMSUNG: SymbolHit = {
  code: "005930",
  name: "삼성전자",
  name_en: "Samsung Electronics",
  market: "KOSPI",
};

describe("SymbolSearch", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("commitOnType 기본값(true): 숫자를 타이핑하는 즉시 각 부분 문자열로 onChange 를 호출한다", () => {
    vi.spyOn(api, "searchSymbols").mockResolvedValue([]);
    const onChange = vi.fn();
    render(<SymbolSearch value="" onChange={onChange} />, { wrapper: makeWrapper() });

    const input = screen.getByPlaceholderText(/코드\/종목명 검색/);
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.change(input, { target: { value: "00" } });
    fireEvent.change(input, { target: { value: "005" } });

    // 단일종목 선택 용도: 타이핑 중 매 글자마다 즉시 반영된다(기존 동작 유지).
    expect(onChange).toHaveBeenCalledTimes(3);
    expect(onChange).toHaveBeenNthCalledWith(1, "0");
    expect(onChange).toHaveBeenNthCalledWith(2, "00");
    expect(onChange).toHaveBeenNthCalledWith(3, "005");
  });

  it("commitOnType=false: 타이핑 중에는 onChange 를 호출하지 않는다(append 용도, 부분 문자열 커밋 방지)", () => {
    vi.spyOn(api, "searchSymbols").mockResolvedValue([]);
    const onChange = vi.fn();
    render(<SymbolSearch value="" onChange={onChange} commitOnType={false} />, {
      wrapper: makeWrapper(),
    });

    const input = screen.getByPlaceholderText(/코드\/종목명 검색/);
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.change(input, { target: { value: "00" } });
    fireEvent.change(input, { target: { value: "005" } });
    fireEvent.change(input, { target: { value: "0059" } });
    fireEvent.change(input, { target: { value: "00593" } });
    fireEvent.change(input, { target: { value: "005930" } });

    expect(onChange).not.toHaveBeenCalled();
  });

  it("commitOnType=false: Enter 로 확정하면 완성된 코드로 한 번만 onChange 를 호출한다", () => {
    vi.spyOn(api, "searchSymbols").mockResolvedValue([]);
    const onChange = vi.fn();
    render(<SymbolSearch value="" onChange={onChange} commitOnType={false} />, {
      wrapper: makeWrapper(),
    });

    const input = screen.getByPlaceholderText(/코드\/종목명 검색/);
    fireEvent.change(input, { target: { value: "005930" } });
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("005930");
  });

  it("commitOnType=false: 드롭다운에서 종목을 선택하면 코드로 onChange 를 호출한다", async () => {
    vi.spyOn(api, "searchSymbols").mockResolvedValue([SAMSUNG]);
    const onChange = vi.fn();
    render(<SymbolSearch value="" onChange={onChange} commitOnType={false} />, {
      wrapper: makeWrapper(),
    });

    const input = screen.getByPlaceholderText(/코드\/종목명 검색/);
    fireEvent.change(input, { target: { value: "삼성" } });

    const option = await waitFor(() => screen.getByText("삼성전자"));
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("005930");
  });
});
