"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { API_BASE_URL } from "@/lib/api";

/** WS 로 수신한 이벤트(JSON 파싱 결과)를 처리하는 콜백 타입. */
type Handler = (data: Record<string, unknown>) => void;

// 서버가 인증 실패 시 닫는 close code. 이 경우 재연결하지 않고 로그인으로 보낸다.
const AUTH_FAILED_CODE = 4401;

/**
 * 엔진 실시간 이벤트 WS 구독 훅.
 * 인증은 HttpOnly 쿠키로 처리되므로 토큰을 URL 에 노출하지 않는다.
 * 인증 실패(4401)면 재연결을 멈추고 로그인으로 이동하며, 그 외 끊김만 백오프 재연결한다.
 * 최신 콜백은 ref 로 보관해, 콜백이 매 렌더 바뀌어도 소켓을 재생성하지 않는다.
 * @param onEvent 이벤트 수신 콜백(언마운트 시 소켓은 자동 정리됨)
 */
export function useEventSocket(onEvent: Handler) {
  const router = useRouter();
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout>;
    let closed = false;

    // API_BASE_URL 이 비어 있으면(상대경로/단일 출처 운용) 현재 페이지 출처를
    // 기준으로 WS 주소를 만든다 → 폰이 어떤 주소로 접속해도 같은 호스트로 연결.
    const httpBase =
      API_BASE_URL ||
      (typeof window !== "undefined" ? window.location.origin : "");
    const wsBase = httpBase.replace(/^http/, "ws");

    function connect() {
      // 쿠키는 same-site WS 핸드셰이크에 자동 포함된다(SameSite 정책 준수).
      ws = new WebSocket(`${wsBase}/ws`);
      ws.onopen = () => {
        retry = 0;
      };
      ws.onmessage = (e) => {
        try {
          handlerRef.current(JSON.parse(e.data));
        } catch {
          /* ignore */
        }
      };
      ws.onclose = (e) => {
        if (closed) return;
        if (e.code === AUTH_FAILED_CODE) {
          // 토큰 만료/무효 — 무한 재연결 루프 대신 로그인으로.
          closed = true;
          router.replace("/login");
          return;
        }
        retry = Math.min(retry + 1, 6);
        timer = setTimeout(connect, retry * 1000);
      };
      ws.onerror = () => ws?.close();
    }

    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [router]);
}
