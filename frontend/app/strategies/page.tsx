"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  X,
  Trash2,
  ChevronRight,
  GripVertical,
  Loader2,
  Star,
  Share2,
} from "lucide-react";
import { api, Strategy, StrategyConfig } from "@/lib/api";
import { Nav } from "@/components/Nav";
import { RequireAuth } from "@/components/RequireAuth";
import { StrategyForm } from "@/components/StrategyForm";
import { summarizeConfig } from "@/lib/strategy";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

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

  // 드래그 중인 카드 인덱스와 현재 드롭 대상 인덱스(드롭 위치 하이라이트용).
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);

  // 순서 변경: 재배열한 ID 배열을 서버에 보낸다. 드롭 직후 서버 응답을 기다리면
  // 카드가 원위치로 튀어 보이므로 캐시를 낙관적으로 먼저 갱신한다.
  const reorder = useMutation({
    mutationFn: (orderedIds: number[]) => api.reorderStrategies(orderedIds),
    onSettled: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  const list = strategies.data ?? [];

  /** 재배열된 목록을 캐시에 즉시 반영하고 서버에 저장한다. */
  const commitOrder = (next: Strategy[]) => {
    qc.setQueryData(["strategies"], next);
    reorder.mutate(next.map((s) => s.id));
  };

  /** index 의 항목을 dir(-1=위, +1=아래) 방향으로 한 칸 이동시킨다(키보드 접근용). */
  const move = (index: number, dir: -1 | 1) => {
    const next = [...list];
    const target = index + dir;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    commitOrder(next);
  };

  /** 드래그한 카드를 target 위치로 이동시킨다(그 자리에 삽입). */
  const drop = (target: number) => {
    if (dragIndex === null || dragIndex === target) return;
    const next = [...list];
    const [moved] = next.splice(dragIndex, 1);
    next.splice(target, 0, moved);
    commitOrder(next);
  };

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
          {list.map((s, i) => (
            <StrategyCard
              key={s.id}
              strategy={s}
              index={i}
              isFirst={i === 0}
              isLast={i === list.length - 1}
              onMove={move}
              reordering={reorder.isPending}
              isDragging={dragIndex === i}
              isDropTarget={overIndex === i && dragIndex !== null && dragIndex !== i}
              onDragStart={() => setDragIndex(i)}
              onDragOver={() => setOverIndex(i)}
              onDrop={() => {
                drop(i);
                setDragIndex(null);
                setOverIndex(null);
              }}
              onDragEnd={() => {
                setDragIndex(null);
                setOverIndex(null);
              }}
            />
          ))}
        </div>
      </main>
    </>
  );
}

/**
 * 전략 목록 카드. 클릭하면 상세로 이동하며, 우측 컨트롤로 즐겨찾기·공유·순서변경·삭제를
 * 직접 처리한다. 컨트롤은 카드 링크 위에 겹쳐 있으므로 클릭 시 링크 이동을 막는다.
 * 순서 변경은 그립 핸들 드래그 앤 드롭으로, 키보드 사용자는 핸들에 포커스 후 ↑/↓ 로 한다.
 */
function StrategyCard({
  strategy: s,
  index,
  isFirst,
  isLast,
  onMove,
  reordering,
  isDragging,
  isDropTarget,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: {
  strategy: Strategy;
  index: number;
  isFirst: boolean;
  isLast: boolean;
  onMove: (index: number, dir: -1 | 1) => void;
  reordering: boolean;
  isDragging: boolean;
  isDropTarget: boolean;
  onDragStart: () => void;
  onDragOver: () => void;
  onDrop: () => void;
  onDragEnd: () => void;
}) {
  const qc = useQueryClient();
  // 그립 핸들을 누르고 있을 때만 카드를 draggable 로 만든다 — 카드 본문 텍스트 선택이나
  // 링크 드래그가 순서 변경으로 오동작하지 않게 하기 위함.
  const [dragEnabled, setDragEnabled] = useState(false);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["strategies"] });

  const remove = useMutation({
    mutationFn: () => api.deleteStrategy(s.id),
    onSuccess: invalidate,
  });
  const favorite = useMutation({
    mutationFn: () =>
      s.is_favorite ? api.unfavoriteStrategy(s.id) : api.favoriteStrategy(s.id),
    onSuccess: invalidate,
  });
  const share = useMutation({
    mutationFn: () =>
      s.is_shared ? api.unshareStrategy(s.id) : api.shareStrategy(s.id),
    onSuccess: invalidate,
  });

  /** 카드 링크 이동을 막고 핸들러를 실행하는 래퍼. */
  const stop = (fn: () => void) => (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    fn();
  };

  const handleDelete = stop(() => {
    if (window.confirm(`'${s.name}'을(를) 삭제할까요? 되돌릴 수 없습니다.`)) {
      remove.mutate();
    }
  });

  return (
    <div
      draggable={dragEnabled}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = "move";
        onDragStart();
      }}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        onDragOver();
      }}
      onDrop={(e) => {
        e.preventDefault();
        onDrop();
      }}
      onDragEnd={() => {
        setDragEnabled(false);
        onDragEnd();
      }}
      className={cn(isDragging && "opacity-50")}
    >
      <Link href={`/strategies/${s.id}`} className="group block" draggable={false}>
        <Card
          className={cn(
            "p-4 transition-colors hover:border-primary/40 hover:bg-accent/30",
            isDropTarget && "border-primary ring-1 ring-primary/40",
          )}
        >
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 font-medium">
            {s.name}
            <ChevronRight className="h-4 w-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
          </span>
          <div className="flex items-center gap-1.5">
            {/* 순서 변경: 드래그 핸들(마우스) + ↑/↓ 키(키보드) */}
            <button
              onPointerDown={() => setDragEnabled(true)}
              onPointerUp={() => setDragEnabled(false)}
              onClick={stop(() => {})}
              onKeyDown={(e) => {
                if (e.key === "ArrowUp" && !isFirst && !reordering) {
                  e.preventDefault();
                  onMove(index, -1);
                } else if (e.key === "ArrowDown" && !isLast && !reordering) {
                  e.preventDefault();
                  onMove(index, 1);
                }
              }}
              aria-label={`${s.name} 순서 변경 — 드래그하거나 방향키로 이동`}
              className="cursor-grab touch-none rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground active:cursor-grabbing"
            >
              <GripVertical className="h-4 w-4" />
            </button>
            {/* 즐겨찾기 */}
            <button
              onClick={stop(() => favorite.mutate())}
              disabled={favorite.isPending}
              aria-label={s.is_favorite ? `${s.name} 즐겨찾기 해제` : `${s.name} 즐겨찾기`}
              aria-pressed={s.is_favorite}
              className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent disabled:opacity-50"
            >
              <Star
                className={cn(
                  "h-4 w-4",
                  s.is_favorite && "fill-amber-400 text-amber-400",
                )}
              />
            </button>
            {/* 공유 토글 */}
            <button
              onClick={stop(() => share.mutate())}
              disabled={share.isPending}
              aria-label={s.is_shared ? `${s.name} 공유 해제` : `${s.name} 공유`}
              aria-pressed={s.is_shared}
              className={cn(
                "rounded-md p-1 transition-colors hover:bg-accent disabled:opacity-50",
                s.is_shared ? "text-primary" : "text-muted-foreground",
              )}
            >
              {share.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Share2 className="h-4 w-4" />
              )}
            </button>
            <Badge variant={STATUS_VARIANT[s.status] ?? "muted"}>
              {STATUS_LABEL[s.status] ?? s.status}
            </Badge>
            {s.is_shared && <Badge variant="secondary">공유 중</Badge>}
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
    </div>
  );
}

/**
 * 신규 전략 생성 래퍼. 공용 StrategyForm 으로 입력받아 생성 요청을 보낸다.
 * @param onCreated 생성 성공 시 호출(폼 닫기·목록 갱신용)
 */
function CreateStrategy({ onCreated }: { onCreated: () => void }) {
  const create = useMutation({
    mutationFn: ({
      name,
      config,
      description,
    }: {
      name: string;
      config: StrategyConfig;
      description: string;
    }) => api.createStrategy(name, config, description),
    onSuccess: onCreated,
  });

  return (
    <StrategyForm
      submitLabel="전략 생성"
      pending={create.isPending}
      error={create.isError ? (create.error as Error).message : null}
      onSubmit={(name, config, description) =>
        create.mutate({ name, config, description })
      }
    />
  );
}
