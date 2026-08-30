# 실시간 자동매매 엔진

`backend/engine/` — 24h asyncio 데몬. `python -m engine.main`

## 루프 구성 (`engine/main.py`)

| 루프 | 역할 |
|---|---|
| `_control_loop` | `engine:control` 구독 → 전략 start/stop. **예외를 삼켜야 한다** — 여기서 죽으면 원격제어가 마비된다(§39) |
| `_heartbeat_loop` | `engine:heartbeat` TTL 갱신 |
| `_reconcile_loop` | 체결 정합 정기 점검 |
| `_fill_notice_loop` | 체결 통보 |
| `_recover` | 재기동 시 `engine:active_strategies` 로 운용 전략 복구 |

`_live_gate_allows_start` 가 실전 전환 게이트다 — **표본 부족이면 차단**(fail-closed).

## 러너

| 모듈 | 역할 |
|---|---|
| `base_runner.py` | 공통 러너 골격(폴링·헬스·실패 카운트) |
| `runner.py` | 단일종목 신호 매매(`StrategyRunner`) |
| `rebalance_runner.py` | 다종목 리밸런싱 매매 |
| `rebalance.py` | `compute_rebalance_orders`·`compute_target_weights` — 백테스트와 **parity** 를 맞춰야 하는 순수 로직 |

**백테스트 ↔ 실거래 parity 가 핵심 제약이다.** `rebalance.py::compute_rebalance_orders` 와
`backtest/portfolio.py::_apply_rebalance` 는 같은 규칙(드리프트 밴드, 신규편입·전량청산은
밴드 예외, 매도 선행)을 구현한다. 한쪽만 고치면 백테스트 성과가 실거래와 갈린다.

## 주문 실행 (`executor.py::execute_signal`)

**3중 멱등 방어.** 중복이면 `None` 반환.

```
1) Redis 분산락  lock:order:{idempotency_key}   (nx=True, TTL)
2) DB 중복 검사
3) 포지션 락     lock:position:{user_id}:{symbol}  ← 읽기-판단-주문 직렬화(TOCTOU)
```

새 주문 경로를 만들면 **반드시** 이 세 가지를 통과시키고 `Order.reason`(한국어)을 채운다.

## 리스크 (`risk.py`)

| 함수 | 검사 |
|---|---|
| `evaluate_buy` | 포지션 한도·일일 손실 한도·**관리종목/정리매매 차단** |
| `evaluate_sell` | 보유 검증 |
| `check_daily_loss_limit` | 계정/전략별 일일 손실(`_today_start_utc` 기준) |
| `check_stop_loss` | 손절 |

한도는 `risk_limits` 테이블. `strategy_id=NULL` 행이 **계정 공통 한도**, 전략별 행이 우선.

**관리종목 차단은 fail-open 이다.** `kis_master.management_block_reason` 은 스냅샷이 없거나
`_MAX_STALE_DAYS`(10일)보다 오래되면 **판정하지 않고 연다** — 야간 배치 장애가 매매 전면
중단으로 번지면 안 되고, 종목마스터엔 이 엔진이 거래하지 않는 채권형·ETN 코드도 섞여 있다.
`live_gate` 의 fail-closed 와 방향이 반대인 이유가 이것이다.

## 정합·체결

| 모듈 | 역할 |
|---|---|
| `reconcile.py` | 브로커 응답과 DB 주문 상태 정합. **접수불명 주문** 처리 포함 |
| `fills.py::record_fill` | 체결 기록. 오버셀은 클램프하되 **경보를 낸다**(§35) |
| `halt.py` | 거래정지 실시간 판정(브로커 응답 기준 — 종목마스터 스냅샷은 최대 하루 지연이라 여기 안 씀) |
| `price_feed.py` / `kis_ws.py` | 실시간 시세 WS. 재연결 실패는 알림 발행(§25) |
| `fill_notice.py` | 체결 통보 |
| `alerts.py::publish_alert` | 알림 발행 — `code` + `dedup_window_hours` 로 반복 억제 |

## 조용한 실패를 만들지 말 것

무인 자동매매라 **실패가 조용하면 사용자가 모른다.** 새 배치·루프를 추가하면 실패 경로에
`publish_alert(code=...)` 를 반드시 붙인다. 기존 code 예:
`runner_failures`·`pit_fallback`·`mdd_kill`·`factor_outage`·`kis_master_outage`·
`db_backup_stale`·`ohlcv_ingest_failure_rate`.

## KIS 연동 (`app/services/kis/`)

- 토큰 발급·갱신은 락으로 직렬화.
- REST 유량제한 재시도는 **시세 조회뿐 아니라 주문 경로에도** 적용된다(§27 에서 비대칭 해소).
- `KIS_ENV=vts` 는 모의투자, `prod` 는 실계정.
