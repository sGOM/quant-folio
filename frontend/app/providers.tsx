"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * 전역 클라이언트 프로바이더. TanStack Query 클라이언트를 1회 생성해 앱 전체에 제공한다.
 * 기본 옵션: 실패 시 1회 재시도, 창 포커스 시 자동 refetch 비활성.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
