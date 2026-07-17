import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useSymbolNames } from "@/lib/useSymbolNames";

/** 테스트마다 격리된 QueryClient 로 감싼 래퍼(캐시가 테스트 간 새지 않도록). */
function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useSymbolNames", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("로딩 → 성공 순서로 상태가 전이되고, nameOf 가 '이름(코드)' 형태를 반환한다", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ "005930": "삼성전자" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const { result } = renderHook(() => useSymbolNames(), {
      wrapper: makeWrapper(),
    });

    expect(result.current.names.isPending).toBe(true);
    // 데이터가 없는 동안은 코드만 그대로 반환한다.
    expect(result.current.nameOf("005930")).toBe("005930");

    await waitFor(() => expect(result.current.names.isSuccess).toBe(true));

    expect(result.current.nameOf("005930")).toBe("삼성전자(005930)");
    // 매핑에 없는 코드는 코드만 반환한다.
    expect(result.current.nameOf("999999")).toBe("999999");
    // null/undefined 는 빈 문자열로 안전 처리된다.
    expect(result.current.nameOf(null)).toBe("");
    expect(result.current.nameOf(undefined)).toBe("");
  });

  it("요청이 실패하면 isError 상태가 되고, nameOf 는 코드 폴백을 계속 반환한다", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "서버 오류" }, 500));
    global.fetch = fetchMock as unknown as typeof fetch;

    const { result } = renderHook(() => useSymbolNames(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.names.isError).toBe(true));
    expect(result.current.nameOf("005930")).toBe("005930");
  });
});
