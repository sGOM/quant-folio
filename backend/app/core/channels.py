"""web ↔ engine 간 Redis 통신 규약 (채널·키 상수).

웹 서버와 매매 엔진은 물리적으로 분리되어 있으므로, 모든 제어/이벤트는
Redis pub/sub 와 키로 주고받는다. 양측이 이 모듈을 공유해 규약을 일치시킨다.
"""

# 전략 ON/OFF 제어 — web 이 발행, engine 이 구독
# 메시지(JSON): {"action": "start"|"stop", "strategy_id": int}
ENGINE_CONTROL_CHANNEL = "engine:control"

# 현재 운용 중(live) 전략 ID 집합 (Redis SET) — 엔진 재기동 시 복구용
ACTIVE_STRATEGIES_KEY = "engine:active_strategies"

# 엔진 → web 실시간 이벤트 (체결/주문/포지션 변동·이상 알림) — web 이 구독해 WS 푸시
# 메시지(JSON): {"type": "order"|"execution"|"position"|"signal"|"alert", "user_id": int, ...}
#   - "alert": 무인 자동매매의 조용한 실패를 사용자에게 즉시 알리는 앱 내 경보.
#     추가 필드: strategy_id:int, severity:"warning"|"critical", message:str(한국어), ts:iso,
#     code:str(알림 종류 식별자, 예 "runner_failures"|"pit_fallback"|"mdd_kill"|"factor_outage"
#     |"db_backup_failed"|"db_backup_stale"|"db_backup_s3_upload_failed"|"ohlcv_ingest_failure_rate").
ENGINE_EVENTS_CHANNEL = "engine:events"


def engine_events_channel(user_id: int) -> str:
    """사용자별 이벤트 채널. 각 WS 소켓이 자기 사용자 채널만 구독한다."""
    return f"{ENGINE_EVENTS_CHANNEL}:{user_id}"


# 로그인 세션 키 프리픽스 — session:{sid} → user_id, TTL=SESSION_TTL_MINUTES
SESSION_PREFIX = "session:"

# 엔진 생존 신호 (TTL 키)
ENGINE_HEARTBEAT_KEY = "engine:heartbeat"

# 전략별 러너 헬스 상태 프리픽스 — engine 이 갱신, web 이 조회.
# 값(JSON): {"strategy_id": int, "user_id": int, "last_success_ts": iso|None,
#            "consecutive_failures": int, "last_error": str|None, "updated_at": iso}
ENGINE_HEALTH_PREFIX = "engine:health:"

# 러너 연속 실패가 이 횟수에 도달하면 "alert" 이벤트를 1회 발행한다(임계 초과 알림).
# poll 주기(30~60초)·틱 타임아웃(120~300초)을 감안하면 3회는 단발 네트워크 블립을
# 걸러내면서도 수 분 내 지속 장애를 조기에 잡아내는 절충값이다. web/engine 이 공유해
# 헬스 조회의 healthy 판정과 알림 임계를 동일 기준으로 맞춘다.
FAILURE_ALERT_THRESHOLD = 3


def engine_health_key(strategy_id: int) -> str:
    """전략별 러너 헬스 상태 키."""
    return f"{ENGINE_HEALTH_PREFIX}{strategy_id}"

# 주문 멱등성 분산 락 프리픽스
ORDER_LOCK_PREFIX = "lock:order:"

# 종목 포지션 직렬화 락 프리픽스 — 같은 (user, symbol) 의 읽기-판단-주문을 직렬화해
# 이중 매수/매도(TOCTOU)를 방지한다.
POSITION_LOCK_PREFIX = "lock:position:"


def position_lock_key(user_id: int, symbol: str) -> str:
    return f"{POSITION_LOCK_PREFIX}{user_id}:{symbol}"


# 주문 단위 체결 정합 직렬화 락 프리픽스 — 같은 주문의 체결을 여러 경로가 동시에
# 반영하는 것을 막는다. 체결 반영 경로가 셋(executor 즉시체결·reconcile 주기 폴링·
# fill_notice 실시간 체결통보)이라, reconcile 폴링과 실시간 체결통보가 같은 주문의
# executions 합을 커밋 전에 동시에 읽어 같은 체결을 이중 기록·이중 가산할 수 있다.
# 두 경로가 이 키를 공유(SET NX)해 주문 단위로 상호 배제된다.
RECONCILE_LOCK_PREFIX = "reconcile:lock:"
RECONCILE_LOCK_TTL = 30  # 초


def reconcile_lock_key(order_id: int) -> str:
    """주문 단위 체결 정합 락 키(reconcile 폴링·실시간 체결통보 공유)."""
    return f"{RECONCILE_LOCK_PREFIX}{order_id}"


# 마지막 DB 백업 성공 시각(ISO) — worker.backup_database 가 기록, worker.check_backup_freshness
# (§9)·엔진 헬스 API 가 조회. TTL 없이 최신값만 유지.
BACKUP_LAST_SUCCESS_KEY = "backup:last_success_at"

# 전략별 MDD 킬스위치 상태(고점 HWM·발동 여부·발동일) 프리픽스 — engine.rebalance_runner
# 가 갱신, /api/engine/strategies/health(§23 후속)가 조회.
# 값(JSON): {"hwm": float, "killed": bool, "kill_date": "YYYY-MM-DD"|None}
MDD_STATE_PREFIX = "rebalance:mdd:"


def mdd_state_key(strategy_id: int) -> str:
    """전략별 MDD 킬스위치 상태 키."""
    return f"{MDD_STATE_PREFIX}{strategy_id}"
