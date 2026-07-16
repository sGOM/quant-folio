"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Bell, ShieldAlert, X } from "lucide-react";
import { useEventSocket } from "@/lib/useWebSocket";
import { cn } from "@/lib/utils";
import { formatRelativeTime } from "@/lib/format";
import type { AlertCode, AlertSeverity } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

/** WS "alert" 이벤트를 클라이언트에서 다루기 좋게 정규화한 형태. */
interface AlertItem {
  /** 안정적인 React key 겸 dedup 용 클라이언트 생성 id. */
  id: string;
  strategy_id: number | null;
  severity: AlertSeverity;
  message: string;
  ts: string;
  code: AlertCode | string;
}

/** sessionStorage 키 — 탭 내 페이지 이동(RequireAuth 리마운트)에도 최근 알림을 보존한다. */
const STORAGE_KEY = "quantfolio:alerts";
/** 보관할 최대 알림 개수(오래된 것부터 잘림). */
const MAX_ALERTS = 30;
/** 신규 알림 토스트가 자동으로 사라지기까지의 시간. 목록에는 계속 남는다. */
const TOAST_TTL_MS = 8000;

/** 코드 → 사람이 읽을 라벨. */
const CODE_LABEL: Record<string, string> = {
  runner_failures: "러너 연속 실패",
  mdd_kill: "MDD 킬스위치",
  pit_fallback: "PIT 유니버스 폴백",
  factor_outage: "팩터 조회 장애",
  fill_quality_drift: "체결 정합 이탈",
};

function loadStored(): AlertItem[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/**
 * 엔진 실시간 알림(WS type="alert") 센터.
 * - 신규 알림은 화면 우하단 토스트로 즉시 노출(자동 소멸)하고,
 * - 동시에 종 모양 버튼의 목록에 계속 쌓아 두어(세션 내 보존) 놓친 알림도 나중에 확인할 수 있게 한다.
 * - severity 에 따라 critical=위험(빨강)/warning=경고(주황) 색으로 구분한다.
 * 화면 콘텐츠를 가리지 않도록 fixed 코너에만 배치한다.
 */
export function AlertCenter() {
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [toasts, setToasts] = useState<AlertItem[]>([]);
  const [open, setOpen] = useState(false);
  const seq = useRef(0);
  const hydrated = useRef(false);

  // 세션 내 이전 페이지에서 쌓인 알림 복원.
  useEffect(() => {
    setAlerts(loadStored());
    hydrated.current = true;
  }, []);

  // 변경될 때마다 저장(최초 복원 이전엔 빈 배열로 덮어쓰지 않도록 가드).
  useEffect(() => {
    if (!hydrated.current) return;
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(alerts));
    } catch {
      /* 저장 용량 초과 등은 무시(알림 이력은 부가 기능) */
    }
  }, [alerts]);

  useEventSocket((data) => {
    if (data.type !== "alert") return;
    const id = `${Date.now()}-${seq.current++}`;
    const item: AlertItem = {
      id,
      strategy_id: typeof data.strategy_id === "number" ? data.strategy_id : null,
      severity: data.severity === "critical" ? "critical" : "warning",
      message: typeof data.message === "string" ? data.message : "새 알림이 도착했습니다.",
      ts: typeof data.ts === "string" ? data.ts : new Date().toISOString(),
      code: typeof data.code === "string" ? data.code : "",
    };
    setAlerts((l) => [item, ...l].slice(0, MAX_ALERTS));
    setToasts((l) => [item, ...l].slice(0, 4));
    setTimeout(() => {
      setToasts((l) => l.filter((t) => t.id !== id));
    }, TOAST_TTL_MS);
  });

  function dismissToast(id: string) {
    setToasts((l) => l.filter((t) => t.id !== id));
  }

  function clearAll() {
    setAlerts([]);
  }

  const criticalCount = alerts.filter((a) => a.severity === "critical").length;

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
              {alerts.length > 0 && (
                <Button variant="ghost" size="sm" onClick={clearAll} className="h-7 px-2 text-xs">
                  모두 지우기
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
                  <AlertListRow key={a.id} item={a} />
                ))}
              </ul>
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
          criticalCount > 0
            ? "border-status-bad/40 bg-status-bad/15 text-status-bad"
            : "border-border bg-card text-foreground hover:bg-accent",
        )}
      >
        <Bell className="h-5 w-5" />
        {alerts.length > 0 && (
          <span
            className={cn(
              "absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full px-1 text-[10px] font-semibold text-white",
              criticalCount > 0 ? "bg-status-bad" : "bg-amber-500",
            )}
          >
            {alerts.length > 99 ? "99+" : alerts.length}
          </span>
        )}
      </button>
    </>
  );
}

/** 자동 소멸형 토스트 1건. */
function AlertToast({ item, onDismiss }: { item: AlertItem; onDismiss: () => void }) {
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

/** 이력 패널의 알림 한 줄. */
function AlertListRow({ item }: { item: AlertItem }) {
  const critical = item.severity === "critical";
  return (
    <li
      className={cn(
        "rounded-md border px-2.5 py-2 text-xs",
        critical ? "border-status-bad/30 bg-status-bad/5" : "border-amber-500/30 bg-amber-500/5",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "font-medium",
            critical ? "text-status-bad" : "text-amber-400",
          )}
        >
          {CODE_LABEL[item.code] ?? "엔진 알림"}
        </span>
        <span className="shrink-0 text-muted-foreground" title={new Date(item.ts).toLocaleString("ko-KR")}>
          {formatRelativeTime(item.ts)}
        </span>
      </div>
      <p className="mt-1 leading-relaxed text-foreground">{item.message}</p>
      {item.strategy_id != null && (
        <p className="mt-0.5 text-[10px] text-muted-foreground">전략 #{item.strategy_id}</p>
      )}
    </li>
  );
}
