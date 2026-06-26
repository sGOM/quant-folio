"use client";

import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type UserOut } from "@/lib/api";

/** 현재 사용자(쿠키 세션) 조회. 401 이면 isError. */
export function useCurrentUser() {
  return useQuery<UserOut>({
    queryKey: ["me"],
    queryFn: api.me,
    retry: false,
    staleTime: 30_000,
  });
}

/** 로그아웃 — 서버 쿠키 폐기 + 전체 쿼리 캐시 정리 후 로그인으로 이동. */
export function useLogout() {
  const router = useRouter();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.logout,
    onSettled: () => {
      qc.clear(); // 이전 사용자 데이터 잔존 방지
      router.replace("/login");
    },
  });
}
