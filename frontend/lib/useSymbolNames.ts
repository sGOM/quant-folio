"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * 종목코드 → 한글명 매핑 조회 훅(monitor·전략 상세가 공유).
 *
 * 거의 불변이라 오래(1시간) 캐시하되 Infinity 는 피한다 — 서버가 최초에 불완전한
 * 맵(외부 소스 일시 실패)을 준 경우 세션 내내 코드만 표시되는 문제를 막고, 서버
 * 자가복구 후 갱신되도록 한다.
 */
export function useSymbolNames() {
  const names = useQuery({
    queryKey: ["symbol-names"],
    queryFn: api.symbolNames,
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: true,
  });
  // "종목명(코드)" 형태로 표기. 이름을 모르면 코드만 반환한다.
  const nameOf = (code?: string | null) => {
    const c = code == null ? "" : String(code);
    const n = names.data?.[c];
    return n ? `${n}(${c})` : c;
  };
  return { names, nameOf };
}
