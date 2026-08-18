"use client";

import { useEffect } from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * App Router 최상위 세그먼트 에러 바운더리. 컴포넌트 렌더 중 예외(백엔드 응답 스키마
 * 변경으로 인한 undefined 접근 등)가 나면 Next.js 가 전체 화면 대신 이 컴포넌트를 보여준다.
 * `reset()` 은 세그먼트를 다시 렌더해 재시도한다 — 서버 상태(TanStack Query 캐시 등)는
 * 그대로 유지되므로, 일시적 렌더 오류(예: 낙관적 업데이트 중 부분 데이터)면 재시도로 복구된다.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 운영에서는 원격 로깅(Sentry 등)으로 대체할 자리 — 우선 콘솔에 남긴다.
    console.error("[app/error.tsx] 렌더 중 예외 발생:", error);
  }, [error]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="flex w-full max-w-sm flex-col items-center gap-4 rounded-lg border border-border/60 bg-card p-6 text-center shadow-sm">
        <AlertTriangle className="h-8 w-8 text-destructive" />
        <div className="space-y-1">
          <p className="text-sm font-medium text-foreground">
            화면을 표시하는 중 오류가 발생했습니다.
          </p>
          <p className="text-xs text-muted-foreground">
            일시적인 오류일 수 있습니다. 다시 시도해도 계속되면 새로고침하거나 잠시 후
            다시 시도해주세요.
          </p>
          {error.digest && (
            <p className="text-[10px] text-muted-foreground/70">
              오류 코드: {error.digest}
            </p>
          )}
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => reset()}>
            다시 시도
          </Button>
          <Button size="sm" onClick={() => window.location.reload()}>
            새로고침
          </Button>
        </div>
      </div>
    </main>
  );
}
