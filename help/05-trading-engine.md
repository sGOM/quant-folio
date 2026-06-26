# 05. 매매 엔진 — 신호에서 주문까지

이 문서의 목표: 독립 프로세스인 **매매 엔진**(`backend/engine`)이 어떻게 24시간 돌며
신호를 만들고, 리스크를 거르고, 중복 없이 주문하는지를 코드로 따라가는 것.
**이 프로젝트의 가장 중요한 백엔드 코드**다.

---

## 1. 엔진의 생애 (`engine/main.py`)

엔진은 web 과 별개의 컨테이너에서 `python -m engine.main` 으로 뜬다. 단일 **asyncio
이벤트 루프** 위에서 여러 코루틴을 동시에 굴린다(스레드 아님).

```
main()
 ├─ 시그널 핸들러 등록 (SIGTERM/SIGINT → 그레이스풀 종료)
 ├─ _recover()         : DB 에서 status=LIVE 인 전략을 찾아 다시 켠다(재기동 복구)
 ├─ _control_loop()    : Redis engine:control 구독 → start/stop 명령 수신 (코루틴)
 ├─ _heartbeat_loop()  : 5초마다 engine:heartbeat 갱신(TTL 15s) (코루틴)
 └─ _shutdown.wait()   : 종료 신호까지 대기 → 모든 러너/피드 정리 후 종료
```

핵심 자료구조:

```python
_runners: dict[int, dict] = {}   # strategy_id -> {task, stop}  실행 중 전략들
_feed_mgr = PriceFeedManager(...) # 사용자별 시세 WS 피드 관리자
```

전략 1개를 켜면(`_start_strategy`):
1. `asyncio.Event`(stop 신호)를 만들고
2. 전략 타입에 맞는 러너(`StrategyRunner` 또는 `RebalanceRunner`)를 생성
3. `asyncio.create_task(runner.run(stop))` 로 **백그라운드 코루틴**으로 띄우고
4. `engine:active_strategies` 집합에 등록(복구·동기화용)
5. `_sync_feeds()` 로 시세 피드를 맞춘다

> Spring 으로 치면: `ConcurrentHashMap<Long, Future>` 에 전략별 비동기 작업을 담고,
> 메시지 리스너가 start/stop 명령으로 작업을 띄우거나 취소하는 데몬.

---

## 2. 전략 1개가 도는 법 (`engine/runner.py` — `StrategyRunner`)

`run()` 은 단순한 폴링 루프다(`_POLL_INTERVAL = 30초`):

```python
async def run(self, stop_event):
    if not await self._load(): return        # 전략·사용자·브로커·시드 로딩
    while not stop_event.is_set():
        await self._tick()                    # 한 번 평가
        await asyncio.wait_for(stop_event.wait(), timeout=30)  # 30초 대기(중단 가능)
```

`_load()` 가 하는 일:
- DB 에서 전략 config 와 사용자 조회, 자격증명 확인
- `make_broker_for_user(user)` 로 증권사 클라이언트 주입(KIS/토스)
- `_seed_series()`: 지표 계산용 **과거 일봉**을 시드. DB(`price_ticks`)에 없으면
  FinanceDataReader 로 적재 후 사용.

### `_tick()` — 한 번의 판단 (핵심 중의 핵심)

```python
async def _tick(self):
    if not is_market_open(): return            # ① 장 운영시간 아니면 스킵
    price = await self._current_price()        # ② 현재가 (Redis 캐시 우선, REST 폴백)

    series = self._series.copy()               # ③ 오늘 봉을 현재가로 갱신해
    series.loc[today] = ...                     #    최신 시계열을 만든 뒤
    sig = latest_signal(series, self._cfg)     # ④ 신호 평가 (백테스트와 같은 함수!)

    async with _position_lock(...) as acquired: # ⑤ (user,symbol) 분산 락으로 직렬화
        if not acquired: return
        held = await self._holding_qty(db)      # 현재 보유 수량

        # ⑥ 청산 우선순위: 리스크 손절 → config 손절/익절/트레일링
        if held > 0 and await risk.check_stop_loss(...): → 전량 매도; return
        if held > 0 and (exit := await self._config_exit(...)): → 전량 매도; return

        # ⑦ 진입/청산 신호 처리
        if sig == "buy" and held <= 0:
            if not (await risk.check_daily_loss_limit(...)).approved: return  # 일일한도
            decision = await risk.evaluate_buy(...)   # 수량 산정(포지션 한도)
            if decision.approved: await execute_signal(... side="buy" ...)
        elif sig == "sell" and held > 0:
            await self._do_sell(...)
```

**기억할 설계 포인트:**
- **현재가 조회 우선순위**: WebSocket 이 채워둔 `price:{symbol}` Redis 캐시를 먼저
  쓰고, 없으면 REST(`broker.get_quote`)로 폴백. → 빠르고, 끊겨도 동작.
- **신호 함수 공유**: `latest_signal(...)` 은 백테스트의 `generate_signals` 와 **같은
  모듈**(`signals.py`). 백테스트=실거래 일관성의 핵심.
- **청산이 진입보다 항상 우선**: 손절/리스크 체크를 먼저 한 뒤에야 신규 매수를 본다.

---

## 3. 멱등성 — "절대 중복 주문하지 않는다" (`engine/executor.py`)

자동매매에서 가장 무서운 건 **같은 신호로 두 번 주문**이 나가는 것(이중 매수 →
2배 손실 위험). 30초 폴링이라 같은 봉을 여러 번 평가할 수도, 엔진이 재기동될 수도,
락이 만료될 수도 있다. 그래서 **3중 방어**를 건다:

```python
# 1) 결정적 멱등성 키 — 같은 신호봉의 같은 주문은 항상 같은 키
key = make_idempotency_key(strategy_id, symbol, side, bar_ts)
#    예: "s3:005930:buy:2026-06-26"

# 2) Redis 분산 락 (SET NX) — 동시 실행 차단
if not await redis.set(f"lock:order:{key}", "1", nx=True, ex=30): return None

# 3) DB UNIQUE 제약 — 최종 방어선
#    orders.idempotency_key 에 UNIQUE → 뚫려도 IntegrityError 로 흡수
try: await db.commit()
except IntegrityError: await db.rollback(); return None
```

3개를 겹치는 이유:
- 키(1)만으론 동시성 못 막음
- 락(2)은 TTL 만료/엔진 재시작 빈틈이 있음
- 그래서 DB UNIQUE(3)가 **무슨 일이 있어도** 같은 키 두 번 INSERT 를 막는다.

> "락은 성능 최적화, **정합성의 진짜 보증은 DB 제약**" — 분산 시스템의 정석.
> Java 로 치면 Redisson 락 + DB unique index 조합과 같은 패턴.

### 주문 후 체결 처리

```python
res = await broker.place_order(symbol, side, qty, order_type="market")  # 시장가 접수
fill_qty, fill_price = await _resolve_fill(broker, order, qty, price)   # 실제 체결 조회
await _record_fill(db, order, fill_qty, fill_price)                      # 기록 + 포지션 갱신
await _publish(redis, {"type":"execution", ...})                        # WS 로 알림
```

**중요**: 시장가라도 신호 시점가와 실제 체결가는 다르다. 그래서 KIS 체결조회
(`get_order_execution`)로 **실제 평균체결가**를 받아 기록한다(`_resolve_fill`).
조회 실패 시에만 신호가로 폴백(경고 로그).

`_record_fill` 의 평균단가 갱신(매수 시):
```python
new_qty = pos.qty + fill_qty
pos.avg_price = (pos.qty * pos.avg_price + fill_qty * price) / new_qty  # 가중평균
```

---

## 4. 두 종류의 락 — 혼동 주의

| 락 | 키 | 막는 것 | 위치 |
|----|-----|---------|------|
| **주문 락** | `lock:order:{idempotency_key}` | 같은 신호의 중복 주문 | executor |
| **포지션 락** | `lock:position:{user}:{symbol}` | 같은 종목의 **읽기-판단-주문 경합**(TOCTOU) | runner |

포지션 락은 "보유수량 읽고 → 살지 판단 → 주문" 이 한 종목에서 **동시에 두 번** 일어나
이중 매수가 나는 걸 막는다(전략이 여러 개 같은 종목을 봐도 직렬화). 주문 락은 그보다
세밀하게 "이 특정 주문" 의 중복을 막는다. 둘은 보완 관계.

---

## 5. 리스크 관리 (`engine/risk.py`)

> 철학: **"리스크를 통과 못 하면 주문 자체를 만들지 않는다."** (안전 우선)

3가지 체크:

| 함수 | 무엇을 | 한도 출처 |
|------|--------|-----------|
| `evaluate_buy` | `max_position_size` 내에서 **매수 수량 산정** | `risk_limits` 테이블 |
| `check_stop_loss` | 평균단가 대비 `stop_loss_pct` 하락 시 손절 트리거 | `risk_limits` |
| `check_daily_loss_limit` | 당일 손익이 `daily_loss_limit` 초과 손실이면 **신규진입 차단** | `risk_limits` |

`_daily_pnl` 은 **당일 실현 손익(체결 현금흐름) + 보유 포지션 평가손익**을 합산한다.
한도 조회(`_get_limit`)는 **전략별 한도 우선, 없으면 사용자 공통 한도** 순으로 찾는다.

config 기반 청산(`runner._config_exit`)도 별도로 있다 — `stop_loss_pct` /
`take_profit_pct` / `trailing_stop_pct`. **트레일링 스탑**은 보유 중 고점을 Redis
(`trail:{strategy}:{symbol}`)에 기록하며 추적하다, 고점 대비 일정% 하락 시 청산.

> `risk_limits`(테이블 기반)와 전략 `config`(JSON 기반) **둘 다** 청산을 거는 구조라,
> 운영자가 건 전역 한도와 전략 자체의 청산 규칙이 함께 작동한다.

---

## 6. 시세 피드 (`engine/price_feed.py`, `engine/kis_ws.py`)

`PriceFeedManager` 가 **사용자당 KIS WebSocket 1개**를 띄워 구독 종목의 실시간
체결가를 받아 `price:{symbol}` Redis 키(TTL 120초)에 쓴다. runner 는 이 캐시를 읽는다.

- 여러 전략이 같은 종목을 봐도 **사용자당 WS 1개로 묶는다**(`ensure` 가 종목집합
  변경 시에만 재구독).
- 연결이 끊기면 `_supervise` 가 **지수 백오프**(5→10→...→120초)로 재연결.
- 토스 같은 WS 미지원 브로커는 피드를 안 띄우고 runner 가 REST 폴링으로 대체.

---

## 7. 장 운영시간 가드 (`app/services/market.py`)

`is_market_open()` 이 **정규장(09:00~15:30 KST) + 영업일**일 때만 True.
휴장일은 pykrx 로 best-effort 확인. `_tick()` 첫 줄에서 이걸 검사해 **장 외 시간엔
신호 평가·주문을 아예 건너뛴다**. (휴장/시간외 오발주 방지)

---

## 8. 토스 등 다른 증권사는? — 브로커 추상화

엔진/러너는 KIS 를 직접 부르지 않고 **`BrokerClient` 인터페이스**(`services/broker/
base.py`)에만 의존한다. `make_broker_for_user(user)` 팩토리가 사용자 설정에 따라
`KisClient` 또는 `TossClient` 를 주입한다.

```python
class BrokerClient(Protocol):     # = Java interface
    async def get_quote(...) -> Quote
    async def place_order(...) -> OrderResult
    async def get_order_execution(...) -> Fill
    async def get_balance(...) -> Balance
```

새 증권사를 붙이려면 이 Protocol 을 구현하는 클라이언트만 추가하면 된다(엔진 무수정).
반환값은 증권사 원시 dict 가 아니라 **정규화 dataclass**(`Quote`, `Fill` 등)로 통일.

> `Protocol` = Python 의 구조적 타이핑 인터페이스. `implements` 키워드 없이 메서드
> 시그니처만 맞으면 그 타입으로 취급된다(덕 타이핑의 타입 안전판).

---

## 다음 단계

실거래 엔진을 봤으니, 그 신호가 어떻게 **백테스트**에서 검증되는지 보자.

→ [06-backtesting-and-signals.md](06-backtesting-and-signals.md)

### 직접 열어볼 파일 (이 순서로)
- `backend/engine/main.py` — 엔진 생애주기.
- `backend/engine/runner.py` — `_tick()` 을 정독. 매매 판단의 전부.
- `backend/engine/executor.py` — 멱등성 3중 방어.
- `backend/engine/risk.py` — 리스크 게이트.
- `backend/app/services/broker/base.py`, `factory.py` — 추상화/주입.
