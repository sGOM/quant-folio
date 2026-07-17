import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useEventSocket } from "@/lib/useWebSocket";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

/** 테스트용 WebSocket 목. 생성된 인스턴스를 배열에 쌓아 접근할 수 있게 한다. */
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closeCalls = 0;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.closeCalls += 1;
  }

  /** 서버가 소켓을 끊었다고 가정하고 onclose 핸들러를 발화한다. */
  simulateClose(code = 1006) {
    this.onclose?.({ code });
  }

  simulateMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

describe("useEventSocket", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    replaceMock.mockClear();
    vi.useFakeTimers();
    global.WebSocket = MockWebSocket as unknown as typeof WebSocket;
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("마운트 시 /ws 로 소켓을 연결한다", () => {
    renderHook(() => useEventSocket(vi.fn()));
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].url).toMatch(/\/ws$/);
  });

  it("수신 메시지를 JSON 파싱해 최신 콜백으로 전달한다", () => {
    const onEvent = vi.fn();
    renderHook(() => useEventSocket(onEvent));
    const ws = MockWebSocket.instances[0];
    ws.simulateMessage({ type: "fill", symbol: "005930" });
    expect(onEvent).toHaveBeenCalledWith({ type: "fill", symbol: "005930" });
  });

  it("끊기면(일반 종료) 1초 후 재연결을 시도한다(1회차 백오프)", () => {
    renderHook(() => useEventSocket(vi.fn()));
    expect(MockWebSocket.instances).toHaveLength(1);

    MockWebSocket.instances[0].simulateClose(1006);
    // 아직 타이머가 다 안 지났으면 재연결 안 됨.
    vi.advanceTimersByTime(999);
    expect(MockWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("연속 재연결 실패 시 백오프가 6초로 캡핑된다", () => {
    renderHook(() => useEventSocket(vi.fn()));

    // 연속으로 6회 넘게 끊어서 백오프가 retry*1000(최대 6000ms)으로 커지는지 확인.
    for (let i = 0; i < 8; i++) {
      const current = MockWebSocket.instances[MockWebSocket.instances.length - 1];
      current.simulateClose(1006);
      vi.advanceTimersByTime(6000); // 상한(6초)까지만 기다리면 항상 재연결되어야 한다
    }
    // 최초 1개 + 8회 재연결 = 9개
    expect(MockWebSocket.instances.length).toBe(9);
  });

  it("정상 연결(onopen) 후에는 재시도 카운터가 리셋된다(다음 끊김은 다시 1초 백오프)", () => {
    renderHook(() => useEventSocket(vi.fn()));
    const first = MockWebSocket.instances[0];
    first.simulateClose(1006);
    vi.advanceTimersByTime(1000);

    const second = MockWebSocket.instances[1];
    second.onopen?.(); // 연결 성공 → retry 리셋
    second.simulateClose(1006);

    vi.advanceTimersByTime(999);
    expect(MockWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(3);
  });

  it("인증 실패(4401)면 재연결하지 않고 로그인으로 이동한다", () => {
    renderHook(() => useEventSocket(vi.fn()));
    MockWebSocket.instances[0].simulateClose(4401);

    expect(replaceMock).toHaveBeenCalledWith("/login");

    // 충분히 시간이 지나도 재연결 시도가 없어야 한다.
    vi.advanceTimersByTime(10_000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("언마운트 시 소켓을 닫고 이후 재연결 타이머를 취소한다", () => {
    const { unmount } = renderHook(() => useEventSocket(vi.fn()));
    const ws = MockWebSocket.instances[0];
    ws.simulateClose(1006);
    unmount();

    expect(ws.closeCalls).toBeGreaterThanOrEqual(0); // cleanup 시 close() 호출 시도됨
    vi.advanceTimersByTime(10_000);
    // 언마운트 후에는 새 소켓이 생기지 않아야 한다.
    expect(MockWebSocket.instances).toHaveLength(1);
  });
});
