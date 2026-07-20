"use client";

import { useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bell, ShieldAlert, X } from "lucide-react";
import { useEventSocket } from "@/lib/useWebSocket";
import { cn } from "@/lib/utils";
import { formatRelativeTime } from "@/lib/format";
import * as api from "@/lib/api";
import type { AlertRecord, AlertSeverity } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

/** WS "alert" 이벤트로 즉시 뜨는 토스트 1건(서버 영속화와 별개, 자동 소멸). */
interface ToastItem {
  id: string;
  strategy_id: number | null;
  severity: AlertSeverity;
  message: string;
  code: string;
}

/** 신규 알림 토스트가 자동으로 사라지기까지의 시간. 알림함(서버 영속화) 목록에는 계속 남는다. */
const TOAST_TTL_MS = 8000;
/** GET /api/alerts 폴링 주기 — WS 이벤트가 즉시 무효화해 주므로 이건 안전망(재연결 공백 등)용. */
const REFETCH_INTERVAL_MS = 60_000;
/** 한 번에 불러오는 알림 수 — "더보기"로 offset을 이만큼씩 넘겨 이전 이력을 이어 붙인다(§21). */
const PAGE_SIZE = 50;

/** 코드 → 사람이 읽을 라벨. */
const CODE_LABEL: Record<string, string> = {
  runner_failures: "러너 연속 실패",
  mdd_kill: "MDD 킬스위치",
  pit_fallback: "PIT 유니버스 폴백",
  factor_outage: "팩터 조회 장애",
  fill_quality_drift: "체결 정합 이탈",
  slippage_calibration_proposed: "슬리피지 캘리브레이션 제안",
  ohlcv_ingest_failure_rate: "일봉 적재 실패율 초과",
  db_backup_failed: "DB 백업 실패",
  db_backup_stale: "DB 백업 신선도 초과",
  db_backup_s3_upload_failed: "DB 백업 오프사이트 업로드 실패",
  live_gate_blocked: "실전 전환 게이트 차단",
};

/**
 * 엔진 알림 센터 — 서버 영속화(alerts 테이블, §17) + WS 실시간 토스트.
 * - 종 버튼은 서버 `unread_count` 배지를 달고, 패널을 열면 최신 50건(본인 + 전역 알림)을
 *   보여준다. 하단 "이전 알림 더보기"로 offset 페이지네이션(§21) 이력을 이어 붙인다.
 *   개별/전체 확인 처리는 서버에 반영되어 다른 세션·재접속에도 유지된다.
 * - WS "alert" 이벤트는 별도로 토스트를 띄우고 목록 쿼리를 무효화해 즉시 반영한다
 *   (미접속 중 발행된 알림도 다음 접속 시 서버 목록에서 놓치지 않고 확인 가능).
 * 화면 콘텐츠를 가리지 않도록 fixed 코너에만 배치한다.
 */
export function AlertCenter() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [open, setOpen] = useState(false);
  const seq = useRef(0);
  const qc = useQueryClient();

  const list = useInfiniteQuery({
    queryKey: ["alerts"],
    queryFn: ({ pageParam }) => api.listAlerts(false, PAGE_SIZE, pageParam),
    initialPageParam: 0,
    // 응답에 has_more 가 없으므로 마지막 페이지가 꽉 찼으면 다음 페이지가 있다고 본다
    // (경계에서 마지막 "더보기"가 빈 페이지 한 번을 받을 수 있으나 무해).
    getNextPageParam: (last, all) =>
      last.items.length === PAGE_SIZE ? all.length * PAGE_SIZE : undefined,
    refetchInterval: REFETCH_INTERVAL_MS,
  });

  const markRead = useMutation({
    mutationFn: (id: number) => api.markAlertRead(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  const markAllRead = useMutation({
    mutationFn: () => api.markAllAlertsRead(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  useEventSocket((data) => {
    if (data.type !== "alert") return;
    const id = `${Date.now()}-${seq.current++}`;
    const item: ToastItem = {
      id,
      strategy_id: typeof data.strategy_id === "number" ? data.strategy_id : null,
      severity: data.severity === "critical" ? "critical" : "warning",
      message: typeof data.message === "string" ? data.message : "새 알림이 도착했습니다.",
      code: typeof data.code === "string" ? data.code : "",
    };
    setToasts((l) => [item, ...l].slice(0, 4));
    setTimeout(() => {
      setToasts((l) => l.filter((t) => t.id !== id));
    }, TOAST_TTL_MS);
    // 서버 영속화(engine/alerts.py::publish_alert)가 같은 이벤트를 DB 에도 적재하므로
    // 목록·배지를 최신화한다.
    qc.invalidateQueries({ queryKey: ["alerts"] });
  });

  function dismissToast(id: string) {
    setToasts((l) => l.filter((t) => t.id !== id));
  }

  // 새 알림 유입으로 offset 이 밀리면 페이지 경계에서 같은 알림이 중복될 수 있어 id 로 걸러낸다.
  const alerts = (() => {
    const seen = new Set<number>();
    const merged: AlertRecord[] = [];
    for (const a of list.data?.pages.flatMap((p) => p.items) ?? []) {
      if (!seen.has(a.id)) {
        seen.add(a.id);
        merged.push(a);
      }
    }
    return merged;
  })();
  const unreadCount = list.data?.pages[0]?.unread_count ?? 0;

  return (
    <>
      {/* 신규 알림 토스트 스택 — 화면 우하단, 종 버튼 위쪽 */}
      <div className="pointer-events-none fixed bottom-20 right-4 z-[60] flex w-80 max-w-[90vw] flex-col-reverse gap-2">
        {toasts.map((t) => (
          <AlertToast key={t.id} item={t} onDismiss={() => dismissToast(t.id)} />
        ))}
      </div>

      {/* 알림 이력 패널(열림 시) */}
      {open && (
        <Card className="fixed bottom-20 right-4 z-[60] flex max-h-[28rem] w-80 max-w-[90vw] flex-col overflow-hidden shadow-lg">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <p className="text-sm font-medium">알림 ({alerts.length})</p>
            <div className="flex items-center gap-1">
              {unreadCount > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => markAllRead.mutate()}
                  disabled={markAllRead.isPending}
                  className="h-7 px-2 text-xs"
                >
                  모두 읽음
                </Button>
              )}
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setOpen(false)}
                aria-label="알림 패널 닫기"
                className="h-7 w-7"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
          <div className="overflow-y-auto p-2">
            {alerts.length === 0 ? (
              <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                아직 알림이 없습니다.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {alerts.map((a) => (
                  <AlertListRow key={a.id} item={a} onMarkRead={() => markRead.mutate(a.id)} />
                ))}
              </ul>
            )}
            {list.hasNextPage && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => list.fetchNextPage()}
                disabled={list.isFetchingNextPage}
                className="mt-1.5 h-7 w-full text-xs text-muted-foreground"
              >
                {list.isFetchingNextPage ? "불러오는 중…" : "이전 알림 더보기"}
              </Button>
            )}
          </div>
        </Card>
      )}

      {/* 종 버튼(fixed) — 화면 콘텐츠를 가리지 않는 코너 배치 */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="알림 목록 열기"
        className={cn(
          "fixed bottom-4 right-4 z-[60] flex h-11 w-11 items-center justify-center rounded-full border shadow-lg transition-colors",
          unreadCount > 0
            ? "border-status-bad/40 bg-status-bad/15 text-status-bad"
            : "border-border bg-card text-foreground hover:bg-accent",
        )}
      >
        <Bell className="h-5 w-5" />
        {unreadCount > 0 && (
          <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-status-bad px-1 text-[10px] font-semibold text-white">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>
    </>
  );
}

/** 자동 소멸형 토스트 1건. */
function AlertToast({ item, onDismiss }: { item: ToastItem; onDismiss: () => void }) {
  const critical = item.severity === "critical";
  return (
    <div
      className={cn(
        "pointer-events-auto animate-fade-in rounded-lg border p-3 text-sm shadow-lg backdrop-blur",
        critical
          ? "border-status-bad/40 bg-status-bad/10 text-foreground"
          : "border-amber-500/40 bg-amber-500/10 text-foreground",
      )}
    >
      <div className="flex items-start gap-2">
        {critical ? (
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-status-bad" />
        ) : (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
        )}
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-muted-foreground">
            {CODE_LABEL[item.code] ?? "엔진 알림"}
            {item.strategy_id != null && ` · 전략 #${item.strategy_id}`}
          </p>
          <p className="mt-0.5 break-words leading-relaxed">{item.message}</p>
        </div>
        <button
          onClick={onDismiss}
          aria-label="알림 닫기"
          className="shrink-0 rounded-md p-0.5 text-muted-foreground hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

/** 이력 패널의 알림 한 줄 — 미확인이면 클릭으로 확인 처리한다. */
function AlertListRow({ item, onMarkRead }: { item: AlertRecord; onMarkRead: () => void }) {
  const critical = item.severity === "critical";
  return (
    <li
      onClick={item.is_read ? undefined : onMarkRead}
      className={cn(
        "rounded-md border px-2.5 py-2 text-xs",
        critical ? "border-status-bad/30 bg-status-bad/5" : "border-amber-500/30 bg-amber-500/5",
        !item.is_read && "cursor-pointer",
        item.is_read && "opacity-60",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 font-medium">
          {!item.is_read && (
            <span
              className={cn(
                "h-1.5 w-1.5 shrink-0 rounded-full",
                critical ? "bg-status-bad" : "bg-amber-500",
              )}
            />
          )}
          <span className={critical ? "text-status-bad" : "text-amber-400"}>
            {CODE_LABEL[item.code ?? ""] ?? "엔진 알림"}
          </span>
        </span>
        <span
          className="shrink-0 text-muted-foreground"
          title={new Date(item.created_at).toLocaleString("ko-KR")}
        >
          {formatRelativeTime(item.created_at)}
        </span>
      </div>
      <p className="mt-1 leading-relaxed text-foreground">{item.message}</p>
      {item.strategy_id > 0 && (
        <p className="mt-0.5 text-[10px] text-muted-foreground">전략 #{item.strategy_id}</p>
      )}
    </li>
  );
}
