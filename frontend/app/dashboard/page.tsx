"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  KeyRound,
  Wallet,
} from "lucide-react";
import { api } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";

/** 대시보드 라우트. 인증 게이트로 감싼 실제 콘텐츠를 렌더한다. */
export default function DashboardPage() {
  return (
    <RequireAuth>
      <DashboardContent />
    </RequireAuth>
  );
}

/** 계정 정보와 KIS 연동 상태 카드를 보여주는 대시보드 본문. */
function DashboardContent() {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const kis = useQuery({
    queryKey: ["kis-health"],
    queryFn: api.kisHealth,
    enabled: !!me.data?.has_kis_credentials,
    retry: false,
  });

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
        <header className="mb-8">
          <h1 className="text-2xl font-semibold tracking-tight">대시보드</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            계정과 KIS 연동 상태를 한눈에 확인하세요.
          </p>
        </header>

        <section className="grid gap-4 sm:grid-cols-2">
          <Card>
            <CardHeader className="flex-row items-center gap-2 space-y-0">
              <Wallet className="h-4 w-4 text-muted-foreground" />
              <CardTitle className="text-sm">계정</CardTitle>
            </CardHeader>
            <CardContent>
              {me.isLoading ? (
                <CardSkeleton rows={3} />
              ) : me.isError ? (
                <ErrorLine text="세션이 만료되었습니다." />
              ) : (
                <div className="divide-y divide-border/60">
                  <Row label="이메일" value={me.data?.email ?? "-"} />
                  <Row
                    label="KIS 계좌"
                    value={me.data?.kis_account_no ?? "미등록"}
                  />
                  <Row
                    label="KIS 자격증명"
                    value={
                      me.data?.has_kis_credentials ? (
                        <Badge variant="success">등록됨</Badge>
                      ) : (
                        <Badge variant="muted">미등록</Badge>
                      )
                    }
                  />
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center gap-2 space-y-0">
              <KeyRound className="h-4 w-4 text-muted-foreground" />
              <CardTitle className="text-sm">KIS 연동 상태</CardTitle>
            </CardHeader>
            <CardContent>
              {!me.data?.has_kis_credentials ? (
                <p className="text-sm text-muted-foreground">
                  설정에서 KIS 모의투자 API 키를 등록하세요.
                </p>
              ) : kis.isLoading ? (
                <CardSkeleton rows={3} />
              ) : kis.isError ? (
                <ErrorLine text="연동 확인 실패" />
              ) : (
                <div className="divide-y divide-border/60">
                  <Row label="환경" value={kis.data?.env ?? "-"} />
                  <Row
                    label="모의투자"
                    value={
                      kis.data?.is_paper_trading ? (
                        <Badge variant="success">예</Badge>
                      ) : (
                        <Badge variant="warning">실전</Badge>
                      )
                    }
                  />
                  <Row
                    label="토큰 발급"
                    value={
                      kis.data?.token_issued ? (
                        <span className="flex items-center gap-1 text-profit">
                          <CheckCircle2 className="h-4 w-4" /> 성공
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-loss">
                          <XCircle className="h-4 w-4" /> 실패
                        </span>
                      )
                    }
                  />
                  {kis.data?.message && (
                    <p className="pt-2 text-xs text-muted-foreground">
                      {kis.data.message}
                    </p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </section>

        <Separator className="my-8" />
        <p className="text-xs text-muted-foreground">
          전략은 상단 메뉴의 “전략”에서 만들고 백테스트할 수 있습니다.
        </p>
      </main>
    </>
  );
}

/** 라벨-값 한 줄 표시. @param label 좌측 라벨 @param value 우측 값 */
function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">{value}</span>
    </div>
  );
}

/** 카드 로딩 자리표시자. */
function CardSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex justify-between">
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-28" />
        </div>
      ))}
    </div>
  );
}

/** 에러 한 줄. */
function ErrorLine({ text }: { text: string }) {
  return (
    <p className="flex items-center gap-1.5 text-sm text-destructive">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      {text}
    </p>
  );
}
