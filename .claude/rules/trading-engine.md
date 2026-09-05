---
paths:
  - "backend/engine/**"
  - "backend/app/services/broker/**"
  - "backend/app/services/kis/**"
  - "backend/app/services/live_gate.py"
---

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
1) 결정적 idempotency_key 생성   (전략·종목·side·신호봉시각 → make_idempotency_key)
2) Redis 분산락 SET NX           lock:order:{idempotency_key}
3) orders.idempotency_key UNIQUE 제약  ← 최종 방어선(IntegrityError 흡수)
   (그 사이에 DB 기존 주문 조회로 한 번 더 거른다)
```

새 주문 경로를 만들면 **반드시** 이 셋을 통과시키고 `Order.reason`(한국어)을 채운다.

### 락은 두 종류다 — 혼동 주의

| 락 | 키 | 막는 것 | 위치 |
|---|---|---|---|
| **주문 락** | `lock:order:{idempotency_key}` | 같은 신호의 중복 주문 | `executor.py` |
| **포지션 락** | `lock:position:{user}:{symbol}` | 같은 종목의 읽기-판단-주문 경합(TOCTOU) | `base_runner.py::_position_lock` |

포지션 락은 멱등 3중 방어의 일부가 **아니라** 보완 관계다. 전략 여러 개가 같은 종목을 봐도
"보유수량 읽고 → 살지 판단 → 주문"을 직렬화해 이중 매수를 막는다.

### 주문 전송 결과는 세 갈래

| 결과 | 상태 전이 |
|---|---|
| 성공 | `SUBMITTED`(주문번호 기록) → 체결 조회 |
| 증권사 명시적 거부(`BrokerError`) | `REJECTED` — 접수 안 됐음이 확인됐으므로 확정해도 안전 |
| **그 밖의 예외**(타임아웃·연결 끊김 등) | **접수 여부 불명** → `PENDING` 유지 + 즉시 알림. `REJECTED` 로 확정하지 않는다 — 실제로 체결됐을 수 있다. `reconcile` 이 잔고로 교차확인한다 |

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
| `reconcile.py` | 미체결·**접수불명(PENDING, 주문번호 없음)** 주문을 실제 체결로 수렴. 접수불명은 체결조회가 불가능해 **잔고 교차확인**으로만 회수하고, 못 밝히면 critical 알림으로 사람에게 넘긴다 — **임의 재주문 절대 금지**(이중 매수). 이미 기록된 체결수량을 뺀 증분만 기록해 멱등 |
| `fills.py::record_fill` | 체결 기록. 오버셀은 클램프하되 **경보를 낸다**(§35) |
| `halt.py` | **시장 CB 상태기계**가 본체 — 동시 정지 비율로 간접 판정하고 `NORMAL→HALTED→COOLDOWN` 을 거친다(CB 해제 직후 붕괴된 호가에 시장가가 꽂히는 것을 막는다). 개별 종목 정지는 브로커 응답(`Quote.halted`)이 판정한다 |
| `price_feed.py` / `kis_ws.py` | 실시간 시세 WS. 재연결 실패는 알림 발행(§25) |
| `fill_notice.py` | 체결 통보 |
| `alerts.py::publish_alert` | 알림 발행 — `code` + `dedup_window_hours` 로 반복 억제 |

## 조용한 실패를 만들지 말 것

무인 자동매매라 **실패가 조용하면 사용자가 모른다.** 새 배치·루프를 추가하면 실패 경로에
`publish_alert(code=...)` 를 반드시 붙인다. 기존 code 예:
`runner_failures`·`pit_fallback`·`mdd_kill`·`factor_outage`·`kis_master_outage`·
`db_backup_stale`·`ohlcv_ingest_failure_rate`.

## 브로커 추상화 (`app/services/broker/`)

특정 증권사에 하드코딩하지 않는다. `SUPPORTED_BROKERS = ("kis", "toss")`, 기본 `kis`.

- **`base.py::BrokerClient`(Protocol)** — `verify_connection` · `get_quote` · `place_order` ·
  `get_order_execution` · `get_balance`. 증권사별 원시 dict 대신 **정규화 dataclass**
  (`Quote`·`OrderResult`·`Fill`·`Balance`)로 통일한다. `Quote.halted` 는 `is_halted_status` 가
  KIS 의 `temp_stop_yn`·`iscd_stat_cls_code` 를 정규화한 것이다.
- **`factory.py::make_broker`** — 자격증명 우선순위: ① 사용자가 앱에서 등록한 DB 값(암호화 저장,
  멀티유저 정식 경로) → ② `.env` 기본값 폴백(단일 운영자 편의).
  컬럼 의미가 브로커마다 다르다: kis = app_key/app_secret/계좌(CANO-PRDT),
  toss = client_id/client_secret/accountSeq.

새 증권사를 붙이면 Protocol 5개 메서드 + 정규화 dataclass 변환만 구현한다. 엔진은 손대지 않는다.

## KIS 연동 (`app/services/kis/`)

- 토큰 발급·갱신은 락으로 직렬화.
- REST 유량제한 재시도는 **시세 조회뿐 아니라 주문 경로에도** 적용된다(§27 에서 비대칭 해소).
- `KIS_ENV=vts` 는 모의투자, `prod` 는 실계정.
