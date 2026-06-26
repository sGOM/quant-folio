"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useCurrentUser } from "@/lib/useAuth";

/**
 * 보호 콘텐츠 게이트. 세션 확인 전에는 로딩만 표시하고, 미인증이면 /login 으로
 * 보낸 뒤 콘텐츠를 렌더하지 않는다. 인증된 경우에만 children 을 렌더한다.
 *
 * 실제 자원 보호는 백엔드 토큰 검증이 담당하며, 이 컴포넌트는 보호 화면의
 * 깜빡임/노출을 막아 UX 를 일원화한다.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { isLoading, isError } = useCurrentUser();

  useEffect(() => {
    if (isError) router.replace("/login");
  }, [isError, router]);

  if (isLoading) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <p className="text-neutral-400">불러오는 중…</p>
      </main>
    );
  }
  if (isError) {
    return <main className="min-h-screen" />;
  }
  return <>{children}</>;
}
