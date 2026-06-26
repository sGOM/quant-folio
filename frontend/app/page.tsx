"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useCurrentUser } from "@/lib/useAuth";

/**
 * 루트 페이지. 세션 유효 여부를 확인해 대시보드 또는 로그인으로 리다이렉트한다.
 */
export default function Home() {
  const router = useRouter();
  const { isLoading, isError, isSuccess } = useCurrentUser();

  useEffect(() => {
    if (isSuccess) router.replace("/dashboard");
    else if (isError) router.replace("/login");
  }, [isSuccess, isError, router]);

  return (
    <main className="flex min-h-screen items-center justify-center">
      <div className="flex items-center gap-2 text-muted-foreground">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-foreground" />
        <span className="text-sm">{isLoading ? "불러오는 중…" : "이동 중…"}</span>
      </div>
    </main>
  );
}
