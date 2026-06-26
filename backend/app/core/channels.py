"""web ↔ engine 간 Redis 통신 규약 (채널·키 상수).

웹 서버와 매매 엔진은 물리적으로 분리되어 있으므로, 모든 제어/이벤트는
Redis pub/sub 와 키로 주고받는다. 양측이 이 모듈을 공유해 규약을 일치시킨다.
"""

# 전략 ON/OFF 제어 — web 이 발행, engine 이 구독
# 메시지(JSON): {"action": "start"|"stop", "strategy_id": int}
ENGINE_CONTROL_CHANNEL = "engine:control"

# 현재 운용 중(live) 전략 ID 집합 (Redis SET) — 엔진 재기동 시 복구용
ACTIVE_STRATEGIES_KEY = "engine:active_strategies"

# 엔진 → web 실시간 이벤트 (체결/주문/포지션 변동) — web 이 구독해 WS 푸시
# 메시지(JSON): {"type": "order"|"execution"|"position"|"signal", "user_id": int, ...}
ENGINE_EVENTS_CHANNEL = "engine:events"


def engine_events_channel(user_id: int) -> str:
    """사용자별 이벤트 채널. 각 WS 소켓이 자기 사용자 채널만 구독한다."""
    return f"{ENGINE_EVENTS_CHANNEL}:{user_id}"


# 로그인 세션 키 프리픽스 — session:{sid} → user_id, TTL=SESSION_TTL_MINUTES
SESSION_PREFIX = "session:"

# 엔진 생존 신호 (TTL 키)
ENGINE_HEARTBEAT_KEY = "engine:heartbeat"

# 주문 멱등성 분산 락 프리픽스
ORDER_LOCK_PREFIX = "lock:order:"

# 종목 포지션 직렬화 락 프리픽스 — 같은 (user, symbol) 의 읽기-판단-주문을 직렬화해
# 이중 매수/매도(TOCTOU)를 방지한다.
POSITION_LOCK_PREFIX = "lock:position:"


def position_lock_key(user_id: int, symbol: str) -> str:
    return f"{POSITION_LOCK_PREFIX}{user_id}:{symbol}"
