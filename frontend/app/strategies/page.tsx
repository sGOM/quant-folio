"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, X, Trash2, ChevronRight, Loader2 } from "lucide-react";
import { api, Strategy, StrategyConfig } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { StrategyForm } from "@/components/StrategyForm";
import { summarizeConfig } from "@/lib/strategy";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

/** 전략 상태 코드 → 한글 배지 라벨. */
const STATUS_LABEL: Record<string, string> = {
  draft: "초안",
  backtested: "백테스트 완료",
  live: "운용 중",
};

/** 전략 상태 코드 → 배지 색상 변형. */
const STATUS_VARIANT: Record<string, BadgeProps["variant"]> = {
  draft: "muted",
  backtested: "secondary",
  live: "success",
};

/** 전략 목록 라우트. 인증 게이트로 감싼 콘텐츠를 렌더한다. */
export default function StrategiesPage() {
  return (
    <RequireAuth>
      <StrategiesContent />
    </RequireAuth>
  );
}

/** 전략 카드 목록과 신규 생성 폼을 보여주는 본문. */
function StrategiesContent() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);

  const strategies = useQuery({
    queryKey: ["strategies"],
    queryFn: api.listStrategies,
  });

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-4 py-8 sm:px-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">전략</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              매매 전략을 만들고 백테스트한 뒤 운용하세요.
            </p>
          </div>
          <Button
            onClick={() => setShowForm((v) => !v)}
            variant={showForm ? "secondary" : "default"}
          >
            {showForm ? (
              <>
                <X className="h-4 w-4" /> 닫기
              </>
            ) : (
              <>
                <Plus className="h-4 w-4" /> 신규 전략
              </>
            )}
          </Button>
        </div>

        {showForm && (
          <CreateStrategy
            onCreated={() => {
              setShowForm(false);
              qc.invalidateQueries({ queryKey: ["strategies"] });
            }}
          />
        )}

        <div className="mt-6 space-y-3">
          {strategies.isLoading &&
            Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-[72px] w-full rounded-lg" />
            ))}
          {strategies.data?.length === 0 && (
            <Card className="flex flex-col items-center justify-center gap-1 border-dashed py-12 text-center">
              <p className="text-sm font-medium">아직 전략이 없습니다.</p>
              <p className="text-sm text-muted-foreground">
                “신규 전략”을 눌러 첫 전략을 만들어 보세요.
              </p>
            </Card>
          )}
          {strategies.data?.map((s) => (
            <StrategyCard key={s.id} strategy={s} />
          ))}
        </div>
      </main>
    </>
  );
}


/**
 * 전략 목록 카드. 클릭하면 상세로 이동하며, 우측 삭제 버튼으로 바로 제거할 수 있다.
 * 삭제 버튼은 카드 링크 위에 겹쳐 있으므로 클릭 시 링크 이동을 막는다.
 * @param strategy 표시·삭제 대상 전략
 */
function StrategyCard({ strategy: s }: { strategy: Strategy }) {
  const qc = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.deleteStrategy(s.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  /** 링크 이동을 막고 확인 후 삭제한다. */
  const handleDelete = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (window.confirm(`'${s.name}'을(를) 삭제할까요? 되돌릴 수 없습니다.`)) {
      remove.mutate();
    }
  };

  return (
    <Link href={`/strategies/${s.id}`} className="group block">
      <Card className="p-4 transition-colors hover:border-primary/40 hover:bg-accent/30">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 font-medium">
            {s.name}
            <ChevronRight className="h-4 w-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
          </span>
          <div className="flex items-center gap-2">
            <Badge variant={STATUS_VARIANT[s.status] ?? "muted"}>
              {STATUS_LABEL[s.status] ?? s.status}
            </Badge>
            <button
              onClick={handleDelete}
              disabled={remove.isPending}
              aria-label={`${s.name} 삭제`}
              className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
            >
              {remove.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Trash2 className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {summarizeConfig(s.config)}
        </p>
        {remove.isError && (
          <p className="mt-1 text-xs text-destructive">
            삭제 실패: {(remove.error as Error).message}
          </p>
        )}
      </Card>
    </Link>
  );
}

/**
 * 신규 전략 생성 래퍼. 공용 StrategyForm 으로 입력받아 생성 요청을 보낸다.
 * @param onCreated 생성 성공 시 호출(폼 닫기·목록 갱신용)
 */
function CreateStrategy({ onCreated }: { onCreated: () => void }) {
  const create = useMutation({
    mutationFn: ({ name, config }: { name: string; config: StrategyConfig }) =>
      api.createStrategy(name, config),
    onSuccess: onCreated,
  });

  return (
    <StrategyForm
      submitLabel="전략 생성"
      pending={create.isPending}
      error={create.isError ? (create.error as Error).message : null}
      onSubmit={(name, config) => create.mutate({ name, config })}
    />
  );
}
