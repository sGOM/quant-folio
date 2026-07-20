"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { API_BASE_URL } from "@/lib/api";

/** WS 로 수신한 이벤트(JSON 파싱 결과)를 처리하는 콜백 타입. */
type Handler = (data: Record<string, unknown>) => void;

// 서버가 인증 실패 시 닫는 close code. 이 경우 재연결하지 않고 로그인으로 보낸다.
const AUTH_FAILED_CODE = 4401;

/**
 * 탭 하나당 소켓 1개만 유지하는 모듈 싱글턴(전체 코드 버그 검사에서 발견 —
 * AlertCenter 와 모니터링 페이지가 각자 useEventSocket 을 호출해 같은 이벤트를
 * 소켓 2개로 중복 수신하고 있었다). 훅 인스턴스는 메시지/인증실패 핸들러만
 * 등록·해제하고, 구독자가 0이 되는 시점에만 실제 소켓을 닫는다.
 */
interface Manager {
  ws: WebSocket | null;
  retry: number;
  timer: ReturnType<typeof setTimeout> | undefined;
  closed: boolean;
  handlers: Set<Handler>;
  authFailedHandlers: Set<() => void>;
}

let manager: Manager | null = null;

function getManager(): Manager {
  if (manager) return manager;

  // API_BASE_URL 이 비어 있으면(상대경로/단일 출처 운용) 현재 페이지 출처를
  // 기준으로 WS 주소를 만든다 → 폰이 어떤 주소로 접속해도 같은 호스트로 연결.
  const httpBase =
    API_BASE_URL ||
    (typeof window !== "undefined" ? window.location.origin : "");
  const wsBase = httpBase.replace(/^http/, "ws");

  const m: Manager = {
    ws: null,
    retry: 0,
    timer: undefined,
    closed: false,
    handlers: new Set(),
    authFailedHandlers: new Set(),
  };

  function connect() {
    // 쿠키는 same-site WS 핸드셰이크에 자동 포함된다(SameSite 정책 준수).
    m.ws = new WebSocket(`${wsBase}/ws`);
    m.ws.onopen = () => {
      m.retry = 0;
    };
    m.ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        for (const h of m.handlers) h(data);
      } catch {
        /* ignore */
      }
    };
    m.ws.onclose = (e) => {
      if (m.closed) return;
      if (e.code === AUTH_FAILED_CODE) {
        // 토큰 만료/무효 — 무한 재연결 루프 대신 로그인으로.
        m.closed = true;
        for (const h of m.authFailedHandlers) h();
        return;
      }
      m.retry = Math.min(m.retry + 1, 6);
      m.timer = setTimeout(connect, m.retry * 1000);
    };
    m.ws.onerror = () => m.ws?.close();
  }

  connect();
  manager = m;
  return m;
}

/**
 * 엔진 실시간 이벤트 WS 구독 훅.
 * 인증은 HttpOnly 쿠키로 처리되므로 토큰을 URL 에 노출하지 않는다.
 * 인증 실패(4401)면 재연결을 멈추고 로그인으로 이동하며, 그 외 끊김만 백오프 재연결한다.
 * 최신 콜백은 ref 로 보관해, 콜백이 매 렌더 바뀌어도 소켓을 재생성하지 않는다.
 * 여러 컴포넌트가 동시에 이 훅을 써도 실제 소켓은 탭당 1개만 유지된다(위 Manager 참고).
 * @param onEvent 이벤트 수신 콜백(언마운트 시 구독만 해제되며, 마지막 구독자면 소켓도 닫힌다)
 */
export function useEventSocket(onEvent: Handler) {
  const router = useRouter();
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    const m = getManager();
    const wrapped: Handler = (data) => handlerRef.current(data);
    const onAuthFailed = () => router.replace("/login");
    m.handlers.add(wrapped);
    m.authFailedHandlers.add(onAuthFailed);
    return () => {
      m.handlers.delete(wrapped);
      m.authFailedHandlers.delete(onAuthFailed);
      if (m.handlers.size === 0) {
        m.closed = true;
        clearTimeout(m.timer);
        m.ws?.close();
        manager = null;
      }
    };
  }, [router]);
}
