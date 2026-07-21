import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { RebalanceFields } from "@/components/strategy-form/RebalanceFields";
import { defaultConfig } from "@/lib/strategy";
import { api, RebalanceConfig, StrategyConfig } from "@/lib/api";

/** 테스트마다 격리된 QueryClient 로 감싼 래퍼(캐시가 테스트 간 새지 않도록). */
function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

/** RebalanceFields 를 상태 보유 래퍼로 감싸 patch 를 실제 config 에 반영해 재렌더한다. */
function Harness({ initial }: { initial: RebalanceConfig }) {
  const [config, setConfig] = useState(initial);
  function patch(p: Partial<StrategyConfig>) {
    setConfig((prev: RebalanceConfig) => ({ ...prev, ...p }) as RebalanceConfig);
  }
  return <RebalanceFields config={config} patch={patch} />;
}

describe("RebalanceFields — 종목 추가(append) 입력", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("종목코드를 직접 타이핑하는 동안에는 부분 문자열이 universe 에 추가되지 않는다", () => {
    vi.spyOn(api, "searchSymbols").mockResolvedValue([]);
    const initial = defaultConfig("rebalance") as RebalanceConfig;
    initial.universe = [];
    render(<Harness initial={initial} />, { wrapper: makeWrapper() });

    const input = screen.getByPlaceholderText(/코드\/종목명 검색/);
    for (const partial of ["0", "00", "005", "0059", "00593", "005930"]) {
      fireEvent.change(input, { target: { value: partial } });
    }

    // 타이핑 중간 부분 문자열들이 무효 코드로 커밋되지 않아야 한다.
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("00")).not.toBeInTheDocument();
    expect(screen.queryByText("005")).not.toBeInTheDocument();
    expect(screen.getByText(/운용 종목 \(universe · 0개\)/)).toBeInTheDocument();
  });

  it("Enter 로 완성된 코드를 확정하면 universe 에 정확히 1개만 추가된다", () => {
    vi.spyOn(api, "searchSymbols").mockResolvedValue([]);
    const initial = defaultConfig("rebalance") as RebalanceConfig;
    initial.universe = [];
    render(<Harness initial={initial} />, { wrapper: makeWrapper() });

    const input = screen.getByPlaceholderText(/코드\/종목명 검색/);
    fireEvent.change(input, { target: { value: "005930" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(screen.getByText(/운용 종목 \(universe · 1개\)/)).toBeInTheDocument();
    expect(screen.getByText("005930")).toBeInTheDocument();
  });
});
