# QuantFolio 다음 개선안

> 장기 방향은 [`docs/ROADMAP.md`](ROADMAP.md). 이 문서는 단기 개선안의 발굴·완료 트래킹과
> **결정 이유**를 담는다. 코드·문서가 `§번호`로 참조하므로 번호는 재사용하지 않는다.
> 새 후보는 문서 끝에 이어서 추가한다.

## 완료 확인 (§1 이전 우선순위)

| 항목 | 근거 |
|------|------|
| 전략 라이프사이클 가드 | 미청산 포지션·가동 중 러너 존재 시 삭제 거부(409) (#53) |
| KIS 실시간 체결통보 | `engine/fill_notice.py` H0STCNI0/9 구독, ORGNO 저장(0009), 델타 멱등 반영 (#54) |
| 전략별 리스크 분리 | 전략별 일일 손실 한도 하드 게이트(계좌 한도와 AND) (#55) |
| 슬리피지 캘리브레이션 UI | fill-quality 제안 필드 + 모니터 승인 UI (#56) |
| 프론트 테스트(유틸·훅·컴포넌트) | `lib/strategy`·`lib/api`(#57), `useSymbolNames`·`useWebSocket`·`TradeLogTable`·`DsrGradeBadge`(#66) |
| 운영 점검 | 실전 전환 게이트·0008 백필 감사 절차 문서화 (#58) |
| 보안·정확성 하드닝 | 브루트포스 지연, 리스크 합산·체결 신뢰성, 러너 루프 블로킹, StrategyForm 분해 (#59) |
| 섹터 집중 한도 | KRX MDC 업종분류 섹터 맵 + `max_sector_pct` (#37, #39) |
| MDD 킬스위치 실거래 | `rebalance_runner._evaluate_mdd_kill` — HWM·전량 청산·`mdd_rearm_days` 쿨다운 (#33) |
| DB 백업 자동화 (구 §6) | worker beat 03:00 KST `pg_dump\|gzip`(volume `db_backups`), 실패 시 critical, `backup:last_success_at`, 14일 보존 |
| OpenDART TTM | 분기 TTM 경로, `financial_period` 옵트인(기본 annual — id=23/24 재현성) (#61) |
| 실측↔백테스트 괴리 추적 | `GET /api/strategies/{id}/tracking` 일별 NAV 재구성·트래킹에러(#62) + 오버레이 차트(#64) |
| 섹터 맵 PIT 스냅샷 | `sector_map_snapshots` + 분기 적재 + `sector_map(as_of=...)`(도입 이전 구간은 근사 폴백) (#63) |
| E2E 스모크 | `frontend/e2e/smoke.spec.ts`, 야간 크론 `e2e-smoke.yml` (#65) |

---

## 1. fill_notice 실계정 검증 (실전 전환 전 필수)

`engine/fill_notice.py` 는 **실계정 미검증 가정 3개**를 안고 있다(모듈 docstring):

- CNTG_QTY 를 "누적 체결수량"으로 간주한 델타 반영 — 증분이면 로직 수정 필요.
- 체결통보에 ORGNO 미노출 가정 — ODNO 단독 매칭.
- `_parse_fill_notice` 필드 인덱스가 공식 샘플 순서 기준.

→ `docs/live-order-guide.md` §2-1-A 체크리스트로 검증. **실계정이 필요해 코드로는 해소 불가.**

## 2. 마이그레이션 0008 백필 감사 (id=23+24 병행 운용 전 필수)

절차는 `docs/live-order-guide.md`. `alembic upgrade head` 후 positions 의 `strategy_id` NULL
잔존 행을 뽑아 수동 귀속 판단. **운영 DB 가 필요해 코드로는 해소 불가.**

---

## 3. TTM 재무 경로 실전 검증 (id=23/24 A/B 백테스트) ✅

`scripts/validate_ttm_ab.py`(2026-07-19), PIT KOSPI200 반기 2-fold, alpha/Sharpe 판정.
**"혼재 — 옵트인 유지"로 종결**: H1(횡보·하락장) TTM 우위, H2(강세장) 연간 우위.
id=23 FULL 연간 우위(Sharpe 1.04/alpha +19.3% vs 0.98/+17.8%), id=24 FULL TTM 근소 우위
(0.88/+12.0% vs 0.86/+11.7%)지만 근거 부족. TTM 은 회전율 +5~7%p. 상세는
`docs/opendart-integration.md` "분기 TTM". config 변경 없음.

## 4. 백테스트 체결 모델 정밀화 — 상하한가·호가단위 ✅

옵트인 `price_limit_model`(기본 False, 재현성) — 체결가를 KRX 호가단위로 라운딩, 전일종가
±30% 도달 방향 주문은 그날 체결 불가로 이월. `_krx_tick_size`/`_round_to_tick`/
`_price_limit_band` + `_apply_rebalance` 의 `prev_prices`.

**A/B(2026-07-19, `validate_price_limit_ab.py`) — "미미, 기존 근사 유지"**: id=23·24 FULL
|Δ| < 0.01%p(임계 |Δalpha|<1%p, |ΔSharpe|<0.05), 체결불가 이월 0건. 소형주·저유동성 전략
등록 시에만 재고.

## 5. 트래킹 NAV 재구성의 사전 포지션 반영 ✅

positions 스냅샷 주입 대신 executions 만으로 완결: `reconstruct_realized_curve` 가 **항상 첫
체결부터 전체 재생**하고 `display_from` 으로 반환 곡선만 자른다(재생엔 `all_execs`, 표시엔
`window_execs`). Position 테이블 무의존이라 §2 없이도 정확 — 체결 기반이 스냅샷보다 신뢰
소스에 가깝다는 판단. 창 이전 체결 건수는 `notes` 에 명시.

## 7. 실체결 기준 회전율·거래대금 보조 노출 ✅

`_apply_rebalance` 가 `(cash, turnover, executed_notional)` 반환 → `avg_turnover_actual`
(ADV 캡·정수주·상하한가 반영 후, 항상 `avg_turnover` 이하). 기존 필드 의미 불변.
`trades[].amount` 는 원래부터 절사 반영 실체결액이었음을 docstring 에 명문화.

---

## 8. 신규 백테스트 옵션·지표의 프론트 노출 ✅

`price_limit_model`·`financial_period` 토글, 실체결 회전율 캡션 노출.
**착수 중 `use_ttm` 이 어디에도 배선돼 있지 않았음을 발견**(테스트 외 호출자 전무 — §3 원문의
"배선만 된 상태"는 부정확). `financial_period` 를 `compute_universe_scores`(라이브·백테스트
공통)까지 스레딩해 실제로 TTM 을 켜게 했다.

## 9. DB 백업 신선도 감시 (last_success_at 소비자 부재) ✅

`worker/tasks.py::check_backup_freshness`(beat 09:00 KST) — `backup:last_success_at`
(`channels.BACKUP_LAST_SUCCESS_KEY` 공유) 부재·26시간 초과 시 critical `db_backup_stale`.
`GET /api/engine/status` 에 `backup_last_success_at`.

**부수 발견 — worker 의 `engine.*` 임포트가 전부 조용히 실패해 왔다.** 커맨드가 콘솔
스크립트 `celery ...` 라 `sys.path[0]` 이 `/usr/local/bin` 이 되어 `/app` 의 `engine` 을 못
찾았다(`app.*` 는 `-A` 로드 시 cwd 가 우연히 꽂혀 살았다). `check_fill_quality_drift` 알림도
한 번도 성공한 적 없었다. `python -m celery ...` 로 해결(`-m` 은 항상 cwd 를 넣는다).

## 10. 백업 오프사이트 복제 ✅

`_upload_backup_to_s3` — pg_dump 성공 후 S3 호환 스토리지(boto3) opt-in 업로드. 버킷·자격증명
중 하나라도 비면(`settings.has_s3_backup`) 건너뜀 — **현재 미설정으로 비활성**(계정 준비는
운영자 몫). 업로드 실패는 로컬 성공을 무효화하지 않고 warning `db_backup_s3_upload_failed`.
설정 절차: `docs/db-backup.md`·`.env.example`·`secrets/README.md`.

## 11. 라이브 자산가치 근사에 실현손익 반영 ✅

`rebalance_runner._live_equity` 의 구 근사(배정자본 + 미실현손익)가 실현손익을 무시해, 손실
라운드트립 후 자산가치가 낙관적으로 리셋되며 MDD 킬·변동성 타겟팅이 과소 발동했다.
`tracking.replay_cash_balance`(체결 전체 재생 → 현금) + 보유 시가평가(시세 실패 시 평단)로 교체.

---

## 12. 야간 일봉 적재 실패 무알림 ✅

`ingest_daily_ohlcv` 실패 종목이 10%(`_INGEST_FAILURE_ALERT_RATIO`) 초과 시 warning
`ohlcv_ingest_failure_rate`. 전략 무관 배치 알림 관례 `user_id=None, strategy_id=0`.

> 이 배치(§12~§16)에서 오탐으로 제외한 후보: live_gate 콜드스타트 순환의존(vts 체결이 표본에
> 자연 누적), `v_low_confidence`·`tracking.notes` 미노출(이미 응답에 있음), `fill_notice.py` 의
> `CancelledError`/`TimeoutError pass`(정상 asyncio 관용구).

## 13. 패닉셀 지표의 문서화된 한계가 API/UI에 미노출 ✅

`panic.py::CAVEATS`(종가 확정 후 판정·장중 V자 미탐지, 브레드스 생존편향, 매매신호 아님 등)를
`PanicOut`·`PanicMarket.caveats` 로 노출, 프론트 접이식 배너(#71).

## 14. 종목 지표 서브스코어(가치·모멘텀·저변동성) 미노출 ✅

`score_value`/`score_momentum`/`score_lowvol` 을 `ScoreCell` 툴팁으로 노출(#71).

## 15. 턴어라운드 스크리너 `smallcap_pct` 조정 불가 ✅

`smallcap_pct`(기본 0.20) 입력 필드 추가(#71).

## 16. 모니터 페이지 오류 메시지·엔진 이벤트 로그 세분화 부족 ✅

스크리너 오류의 백엔드 사유 노출, 모니터 WS 이벤트 페이로드 원문(접이식) 노출(#71).

---

## 17. 알림 유실 경로 2건 — 영속화·수신 보장 부재 ✅

`alerts` 테이블(0011, `user_id` NULL=전역) + `publish_alert` 가 WS·텔레그램과 함께 항상 적재
(영속화 실패는 로그만 — 기존 흐름 불차단). `GET /api/alerts`·`POST /{id}/read`·`/read-all`.
`AlertCenter.tsx` 를 서버 소스로 전환(60초 폴링 + WS 수신 시 무효화). 원안의 "헤더 벨"은 `Nav`
가 9개 페이지에 개별 임포트되는 구조라 기존 전역 마운트(우하단)를 유지했다.

## 18. 백업 신선도의 프론트 미노출 ✅

모니터 페이지에 백업 신선도 배지(§9 와 같은 26시간 임계, 초과·이력 없음이면 경고색).

## 19. 패닉셀 브레드스 S9(신저가 비율) 계산 편입 ✅

`_fetch_market_ohlcv_snapshot` 를 매일 1건씩 캐시에 누적해 트레일링 252거래일 종가를 재구성,
오늘 종가가 윈도우 최저면 신저가. 윈도우 60거래일(`_S9_MIN_WINDOW`) 미만은 결측.
브레드스 가중 30 을 S5/S6/S9 각 10 으로 재배분. **백테스트 롤링(`compute_panic_series`)에는
미연동**(실거래 대시보드 전용) → 과거 점수 재현성 불변. 임계값은 잠정(남은 과제 4).

---

## 20. 팩터 섹터 중립화 (`neutralize="sector"`) — 전제조건 해소로 승격 ✅

`neutralize` 에 `sector`·`size_sector` 추가. 섹터는 범주형이라 OLS 잔차화(사이즈) 대신 **섹터별
demean**(단독 섹터는 정보 소거를 막으려 원값 보존). `size_sector` 는 사이즈 잔차화 후 섹터 demean.
라이브는 `sector_map(as_of)`(PIT) 주입, 백테스트는 `_fundamentals_provider_with_neutralize_cols`.

**id=23 A/B(2026-07-19, `validate_sector_neutralize_ab.py`) — "현행(미적용) 유지"**: `sector` 는
FULL 근소 우위(Sharpe 1.07 vs 1.04)지만 H1 알파 소멸(−0.2% vs +1.4%) → 혼재. `size_sector` 전면
열위. **해석: id=23 의 업종 쏠림은 왜곡이 아니라 방어 알파의 원천**(저변동이 특정 업종에 편중)이라
demean 이 이를 깎는다. 한계: 당시 `sector_map_snapshots` 가 비어 전 구간 현재 분류 폴백.

## 21. alerts 테이블 보존정책·반복 알림 억제 부재 (§17 운영 후속) ✅

- `cleanup_old_alerts`(beat 04:00 KST, 백업 03:00 회피): 읽음 90일·미읽음 180일.
- `publish_alert(dedup_window_hours=)`: 같은 `(user_id, strategy_id, code)` 미확인 알림이 창 안에
  있으면 DB 적재·텔레그램 생략, **WS 토스트는 항상 통과**. `db_backup_stale`·
  `ohlcv_ingest_failure_rate` 에 20시간 창 적용.
- `GET /api/alerts?offset=` + `AlertCenter` `useInfiniteQuery` "더보기"(has_more 는 §23 에서 정확화).

## 22. DSR 동질 시행 집합의 유니버스 식별 부재 ✅

`_universe_fingerprint` — 실제 유니버스(PIT 해소 후, 정렬)+`universe_rule` 의 sha256(16자)을
`result["universe_fingerprint"]` 에 저장. DSR 동질 집합은 기간 필터 후 지문 일치로 좁힌다. 지문
없는 과거 이력은 대상도 지문이 없을 때만 기간 필터로 폴백(하위호환). 스키마 변경 없음(JSONB).

---

## 23. 엔진 상시 루프 무알림·alerts 회귀 방어 공백·has_more 추정 제거 ✅

- **상시 루프 무알림**: `_reconcile_loop`·`_fill_notice_loop` 가 예외를 삼키고 재시도만 해,
  며칠 실패해도 몰랐다(`_reconcile_loop` 가 죽으면 미체결이 영원히 SUBMITTED). 러너의 임계-교차
  1회 알림 패턴(`FAILURE_ALERT_THRESHOLD`, 성공 시 리셋)을 이식, 전역이라 `user_id=None` critical.
- **alerts 테스트 0건 해소**: dedup·보존정책·라우트 13건. 보존정책 테스트는 실제 `delete()` 의
  WHERE 를 `sqlalchemy.orm.evaluator._EvaluatorCompiler` 로 평가해 재구현 오차 없이 검증.
- **has_more**: 라우트가 `limit+1` 조회로 정확히 계산(`AlertListOut.has_more`), 프론트 추정 제거.
- **reconcile 브로커 실패 무계측**: `_broker` 생성 실패(자격증명 만료 등)를 `stats["errors"]` 로
  올려 루프 실패 카운터가 보게 했다.

## 24. MDD 킬스위치 상태 노출 + 배치 태스크 무알림 2건 해소 ✅

- `rebalance:mdd:{id}` 를 아무도 안 읽어 "청산 상태로 재가동 대기 중"을 확인할 수 없었다. 키
  빌더를 `channels.mdd_state_key` 로 옮기고 `GET /api/engine/strategies/health` 에
  `mdd_killed`/`mdd_kill_date`/`mdd_hwm`, 모니터에 "MDD 킬 발동중" 배지.
- `snapshot_sector_map`(조용히 실패하면 PIT 섹터가 최소 3개월 스테일)·`cleanup_old_alerts` 에
  warning 알림(`sector_map_outage`·`alert_cleanup_failed`).

## 25. 실시간 시세 WS 재연결 실패 무알림 해소 ✅

`PriceFeedManager._supervise` 가 백오프(최대 120초) 재연결 반복 실패에도 로그만 남겼다(러너는
REST 폴백이라 안 죽지만 실시간성이 조용히 저하). 사용자별 `_fail_counts` 로 임계-교차 1회
`price_feed_outage`(warning, dedup 6h). 사용자별 피드라 실제 user_id 로 발행.

## 26. `engine/kis_ws.py` 테스트 커버리지 0 해소 ✅

`test_kis_ws.py` 7건 — 구독 메시지·PINGPONG·체결가 파싱과 예외 경로. 실 WS 연결은 범위 밖.

## 27. KIS REST 유량제한 재시도가 시세 조회에만 있던 비대칭 해소 ✅

EGW00201 재시도가 `get_current_price` 에만 있었다. 교차 프로세스(web·engine·worker) 경합은
전역 `_RateLimiter` 로도 못 막고, 주문·체결조회·잔고 실패는 파급이 더 크다(미체결 오판·reconcile
오탐). `_request_json` 공통 헬퍼로 네 메서드 통일. `test_kis_client.py` 5건.

## 28. `TossClient` HTTP 레벨(재시도·토큰 락) 테스트 커버리지 0 해소 ✅

기존 테스트가 `_request` 를 통째로 몽키패치해, 429 Retry-After 재시도·토큰 Redis 락
single-flight·계좌 검증이 한 번도 실행된 적 없었다. **토스는 모의투자가 없어 항상 실거래**라
이 버그는 실주문 실패로 직결된다. `test_toss_client.py` 7건. `_request` 의 "429 재시도 후에도
실패" 메시지는 도달 불가 코드(동작 무영향)라 테스트 주석으로만 기록.

## 29. `app/services/recommend.py` 추천 스코어링 테스트 커버리지 0 해소 ✅

`test_recommend.py` 5건 — 빈 입력, 종가 결측 시 시총/상장주식수 폴백, OpenDART 실패 중립 처리.

## 30. `app/api/routes/ws.py` 실시간 이벤트 중계 테스트 커버리지 0 해소 ✅

`test_ws_events.py` 3건 — 미인증 4401, 중계·종료 시 unsubscribe/aclose, JSON 파싱 실패 스킵.
버그 없음(커버리지 확충).

## 31. `engine/reconcile.py` 체결 정합 핵심 로직 테스트 커버리지 0 해소 ✅

`_process_one` 델타 계산·상태만 stale 보정·모의투자 잔고 폴백(매수 한정·타 주문 소비분 차감,
실전 비활성)·락 보유 시 스킵을 9건으로 고정. 버그 없음.

## 32. `engine/runner.py::StrategyRunner` 단일종목 매매 핵심 로직 테스트 커버리지 0 해소 ✅

`test_strategy_runner.py` 9건 — 손절 우선순위, RiskLimit 손절과 config 청산의 독립 작동,
트레일링 고점 캐시, 포지션 락 경합 스킵, 일일 손실 한도 차단. 인메모리 FakeDB/Broker/Redis 로
실제 `risk.py`/`executor.py` 를 태웠다. 버그 없음.

## 33. `engine/executor.py::execute_signal` 3중 멱등 방어·거부 경로 테스트 커버리지 0 해소 ✅

`test_executor.py` +7건 — 락 경합, DB 기존 주문, `IntegrityError` 흡수, `BrokerError` →
REJECTED, 미체결 SUBMITTED 유지, 체결조회 실패 시 reconcile 위임. 버그 없음.

## 34. `engine/risk.py` evaluate_buy/evaluate_sell/check_stop_loss 순수 유닛 테스트 커버리지 0 해소 ✅

`test_risk_evaluate.py` 15건 — 한도 소진·1주 미만 현금·무효 가격, 전략/계좌 스코프 분기,
`check_stop_loss` 방어, `_aggregate_position` 수량가중 평균. 버그 없음.

## 35. `engine/fills.py::record_fill` 오버셀 무경보 클램프 해소 ✅

매도 체결이 보유를 넘으면 `pos.qty` 가 흔적 없이 0 으로 클램프돼 계정 상태 오류를 은폐할 수
있었다. 선택적 `redis` 인자로 `oversell_clamped` warning 발행(`redis=None` 이면 로그만).
`engine.alerts` 가 `engine.fills` 를 import 하므로 **순환 import 회피용 지연 import**.

## 36. `worker/tasks.py` DB 백업 순수 로직(URL 파싱·보존정책) 테스트 커버리지 0 해소 ✅

`test_backup_tasks.py` 7건 — `_parse_database_url`·`_prune_old_backups`. 버그 없음.

## 37. `app/api/routes/auth.py` 로그인 브루트포스 방어 로직 테스트 커버리지 0 해소 ✅

`test_login_bruteforce.py` 8건 — 이메일 10회·IP 50회 경계, 성공 시 이메일 카운터만 리셋(IP 유지),
**Redis 장애 시 차단 안 함(가용성 우선)**. `conftest.FakeRedis` 에 `incr`/`expire` 추가. 버그 없음.

## 38. `app/core/session.py` 서버측 세션 로직 테스트 커버리지 0 해소 ✅

`test_session.py` 7건 — 슬라이딩 만료, 빈 sid 단락, 오염된 저장값 None 반환. 버그 없음.

> §26~§38 은 "테스트 0건 경로" 발굴 라운드였다. 보류로 남긴 후보: `trading.py::list_positions`
> 브로커 폴백(조회 전용), `strategies.py::_get_owned` IDOR(§60 에서 해소).

---

## 39. `engine/main.py::_control_loop` 예외 미처리로 원격제어 마비 가능 — 수정 ✅

좁은 `except (JSONDecodeError, KeyError, ValueError)` 밖의 예외(Redis 순간 단절)가 나면
`_control_loop` 가 조용히 종료됐다. 하트비트는 별도 태스크라 헬스는 "정상" — **긴급 중지
명령이 무시돼도 모른다.** 넓은 except + 재구독 + 임계-교차 critical 로 수정. `test_control_loop.py` 4건.

## 40. 프론트엔드 WS 중복 연결 + `formatRelativeTime` 도달불가 분기 — 수정 ✅

- **WS 중복**: `RequireAuth` 가 전 화면에 `AlertCenter`(자체 `useEventSocket`)를 마운트하는데
  `/monitor` 도 따로 호출해 소켓 2개로 이벤트를 중복 수신했다. `lib/useWebSocket.ts` 를 **탭당
  소켓 1개 모듈 싱글턴**(구독자 Set, 마지막 해제 시 닫음)으로 재작성.
- **`formatRelativeTime`**: `< 5` 검사가 `< 0` 보다 먼저라 "곧" 분기가 죽은 코드였다. 순서 교정.

당시 Docker 오프라인으로 vitest 는 미실행(tsc 만 확인) — §62 의 전체 vitest 통과(131건)로 해소.

## 남은 과제

| 순위 | 항목 | 이유 |
|------|------|------|
| 1 | fill_notice 실계정 검증 (§1) | 실전 전환의 마지막 관문. 실계정 필요 |
| 2 | 0008 백필 감사 (§2) | id=23+24 병행 운용 전제(§5 로 긴급도 낮아짐). 운영 DB 필요 |
| 3 | S3 오프사이트 백업 자격증명 (§10) | 코드 완료, 버킷·키는 운영자 몫 |
| 4 | 패닉셀 S9 임계값 캘리브레이션 (§19) | 잠정값(warn 0.10/panic 0.25) — 캐시가 쌓인 뒤 역사적 사례로 검증 |

## 41. 기관·외국인 수급(flow) 팩터 — 구현·PIT 검증 후 전략 등록 기각 ⚠️

**가설**: 가격·재무 팩터 소진 후, id=23 과 저상관인 새 return driver 로 외국인+기관 누적 순매수
(60~120일 지속 신호)를 보완재로 추가(financial-expert 설계).

**배선(opt-in 보존, 기본 0)**: `fetch._fetch_net_purchases`(pykrx 순매수 대금, 시장당 1회),
`factors.compute_flow_norm`(누적 순매수 / 시총 또는 거래대금), `score_flow`,
`FactorWeights.flow`·`flow_window`(90)·`flow_denom`.

**검증**(PIT KOSPI200, 2021.1–2025.6, next_close+슬리피지, 왕복≈0.33%): id=23 믹스를 0.8 로
줄이고 flow=0.20. 기준 id=23 FULL ret +130.0% / Sharpe 1.04 / alpha +19.3% / β0.57 / MDD −23.0%
/ 실회전율 106%.

| flow 변형 | FULL alpha | FULL Sharpe | id23 상관 | 50/50 결합 Sharpe |
|---|---|---|---|---|
| w60 mcap | +9.6% | 0.63 | +0.91 | 0.87 |
| w60 value | +9.9% | 0.69 | +0.90 | 0.90 |
| w90 mcap | +11.4% | 0.71 | +0.90 | 0.90 |
| w90 value | +8.8% | 0.62 | +0.90 | 0.87 |
| **w120 mcap** | **+14.5%** | **0.85** | +0.89 | 0.98 |
| w120 value | +7.5% | 0.56 | +0.87 | 0.85 |

**기각**: (1) 양 반기 우위 변형 없음(최선 w120 mcap 도 H2 열위 +26.3% vs +36.3%). (2) id=23 과
상관 +0.87~0.91, 결합 Sharpe 가 전부 단독(1.04) 미만 — 분산 기여 음. (3) 단독 alpha 가 낮아 희석.
**해석**: 대형주 누적 순매수는 기관이 담는 퀄리티·모멘텀 바스켓의 중복 프록시. 회전율은 문제
아니었음(94~125%). 스크립트 `validate_flow_factor.py`.

## 42. 잔차(베타·사이즈 조정) 모멘텀 팩터 — 구현·PIT 검증 후 전략 등록 기각 ⚠️

**가설**: 원시 모멘텀이 id=23 에서 IR −0.18 로 역효과인 건 "저베타 전략에서 모멘텀이 베타 베팅으로
변질"됐기 때문 → 시장 회귀 잔차의 누적(Blitz·Huij·Martens 2011)으로 대체하면 해소될 것.

**배선(opt-in 보존)**: `compute_residual_momentum_panel`(월수익률 롤링 회귀, 잔차 평균/표준편차,
as_of 컷), `score_residual_momentum`(원시 슬롯과 별개), `FactorWeights.residual_momentum`·
`resid_mom_reg_window/window/skip`. 사이즈 조정은 크로스섹션 중립화로.

**검증**(동일 조건, momentum 0.2 를 스왑):

| 잔차 변형 | FULL alpha | FULL Sharpe | FULL MDD | id23 상관 | 결합 Sharpe |
|---|---|---|---|---|---|
| r36 w11 s1 | +11.8% | 0.80 | −20.6% | +0.91 | 0.95 |
| **r24 w11 s1** | **+13.4%** | **0.86** | −20.1% | +0.91 | 0.98 |
| r36 w6 s1 | +11.2% | 0.76 | −26.7% | +0.92 | 0.93 |
| r24 w6 s1 | +10.3% | 0.69 | −27.4% | +0.90 | 0.90 |

| 팩터 단독 | IC | IR | 롱숏수익 |
|---|---|---|---|
| 원시 모멘텀 | −0.033 | −0.27 | −0.251 |
| 잔차 r24 w11 | −0.056 | −1.41 | −0.424 |
| 잔차 r36 w11 | −0.081 | −1.38 | −0.418 |

**기각**: (1) **가설 반증** — 잔차화하자 음(−) IC 가 **증폭**됐다. 이 유니버스에서 모멘텀의 음
예측력은 베타가 아니라 **고유 반전**이고, 잔차화는 그 반전을 순화한다. (2) 4변형 전부 양 반기
열위. (3) 상관 +0.90~0.92, 결합 Sharpe 전부 단독 미만. 스크립트 `validate_residual_momentum.py`.

## 43. PEAD(실적 서프라이즈 드리프트) 팩터 — 구현·PIT 검증 후 전략 등록 기각 ⚠️

**가설**: 컨센서스가 없으니 기대치를 계절적 랜덤워크(전년 동기)로 두고 SUE 를 `score_pead` 로.

**배선(opt-in 보존)**: 핵심은 **접수일(rcept_dt) 기준 PIT 정렬** — `opendart.disclosure_calendar`
가 정기공시 실제 접수일을 파싱(정정공시는 최초 접수일 유지), `pead_sue_by_symbol` 은
rcept_dt≤as_of 보고서만 취해 단일분기 순이익 YoY 를 lookback_q 분기 표준편차로 표준화.
`FactorWeights.pead`·`pead_lookback_q`(8). `tests/test_pead_factor.py` 17건이 미래참조 차단을
못박는다(as_of 이후 분기를 ±1e9 로 뒤집어도 SUE 불변, 접수일 당일 포함·다음날 배제).

**부수 버그 2건 수정(존치 — 다른 DART 팩터에도 이로움)**: (1) `portfolio.py` 팩터 컬럼
화이트리스트에 `pead_sue` 누락 → 조용히 드롭돼 pead 변형이 id=23 과 완전 동일하게 나왔다.
(2) `opendart` 파생결과 캐시가 **조회 실패의 all-None 결과까지 캐시**해 일시 실패가 프로세스
수명 동안 굳었다("실패는 캐시하지 않는다" 규약이 `_ACCOUNTS_CACHE` 에만 적용돼 있었음).

**검증**(PIT 269종목 합집합, 동일 조건, pead=0.20):

| pead 변형 | FULL alpha | FULL Sharpe | id23 상관 | 결합 Sharpe | 단독 IC / IR |
|---|---|---|---|---|---|
| lb6  | +12.8% | 0.76 | +0.96 | 0.91 | −0.015 / −0.24 |
| lb8  | +15.6% | 0.89 | +0.96 | 0.98 | −0.013 / −0.20 |
| lb12 | +16.4% | 0.92 | +0.97 | 0.99 | −0.013 / −0.19 |

**기각**: (1) SUE 프록시 단독 IC 가 약한 음 — 예측력 없음. (2) 상관 +0.96~0.97(§41·§42 보다 높음),
결합 Sharpe 전부 단독 미만. (3) 양 반기 우위 없음. 캐던스 A/B: 월간은 id=23 자체가 분기보다 크게
열위(alpha +11.7%/Sharpe 0.67) — 캐던스가 본질이 아니라 프록시 예측력 부재가 문제(월간 pead
lb12 줄은 세션 중단으로 미확보, 결론 확정적이라 재실행 안 함). 스크립트 `validate_pead_factor.py`.

---

> **2026-07-31 배치(§44~§47)**: 2026-06~07 KRX 폭락(7월 −22.19%, 6월 고점 대비 장중 −43.9%,
> 7-31 하루 +17.91%)을 "지표로 미리 알고 대처할 수 있었나"로 코드베이스에 대조해 나온 공백.
> 데이터 소스 규격은 `app/services/data/kofia.py` docstring.

## 44. 엔진에 거래정지·시장 서킷브레이커 개념 부재 — 해소 ✅

7월 시장 CB 9회 발동(매번 20~30분 체결 불가, 재개 직후 호가 붕괴) 중에도 러너가 주문을 냈다.
**원인은 데이터 유실**: KIS `inquire-price` 의 `temp_stop_yn`·`iscd_stat_cls_code` 를
`Quote` 정규화가 버리고 있었다.

**구현(PR #109)**: `Quote.halted`·`status_code` + `is_halted_status()`(브로커 계층 단일 출처).
`engine/halt.py` — 동시 정지 비율로 시장 CB 를 간접 판정하는 상태기계(`NORMAL→HALTED→COOLDOWN`),
게이트는 `base_runner._place`. 설계 결정:
- **COOLDOWN**: 붕괴된 호가에 시장가가 꽂히는 것이 정지보다 위험하다.
- **표본 부족 시 판정 보류**(`min_sample`): 소수 종목 전략에서 개별 VI 를 시장 CB 로 오판하면 정상장
  매매가 멈춘다. 그래서 러너별이 아닌 프로세스 전역 모니터로 관측을 합친다.
- **러너는 두고 주문만 막는다**: 러너를 멈추면 관측도 멈춰 재개를 타이머로만 판단하게 된다. 시세
  조회 실패는 '정지 모름'이라 통과.

**남은 검증**: `temp_stop_yn='Y'`·상태코드 58 은 문서 기준값, **실계좌 미교차확인**. 특히 시장 CB
중 개별 종목에 `temp_stop_yn=Y` 가 오는지가 간접 판정의 전제 — 틀리면 시장 CB 감지는 무력(종목
정지 게이트는 유효). 모의 계좌로 VI 종목 조회해 확인할 것.

## 44-1. KRX 로그인 재시도 폭주로 인증 차단 — 해소 ✅

`_build_pit_pool` 이 월마다 `index_members` 를 부르고 세션 무효 시마다 로그인해, 19개월 백테스트
한 번에 로그인 19회 → KRX 가 차단 페이지(HTML)를 반환. **더 위험한 건 조용함**: 모든 PIT 조회가
0종목 → 백테스트가 **빈 패널 위에서 '성공'**했다(실제로 쓰레기값을 받았다).

**수정**: (a) 로그인 실패 시 300초 쿨다운('예외 없이 None' 경로도 실패로), (b) PIT 후보풀 전 구간
0종목이면 에러 로그, (c) 검증 스크립트는 빈 패널이면 중단. 구조적 해소는 §48. 월별 PIT 조회 반복은 §49 로컬 저장
(`index_constituents`)으로 최초 적재 이후엔 재로그인 없이 읽힌다.

## 45. 사전(취약성) 지표 계층 부재 — 데이터 수집만 완료 ⚠️

`panic.py` S1~S9 는 가격·브레드스 기반 **동시지표**(바닥 판정용, 사전 경보 아님). 레버리지(원인)는
가격이 오르는 동안 몇 달에 걸쳐 쌓이므로 관측 대상이 다르다.

**수집 완료(PR #110)**: `kofia.py` — FreeSIS 증시자금(미수금·반대매매) 일별, 무인증·**2008년까지
소급**. 컬럼 라벨이 없어 산술관계로 식별(비중 = 반대매매[t]/미수금[t−1]×100, 41/41 성립), 의미
미확정 컬럼은 `raw` 로만. 신용융자는 별도 표(`...070BO`, `TMPV2 = TMPV3 + TMPV4` 42/42, 2026-06-01
37.68조 → 07-30 32.15조), 레버리지 ETF 는 KRX 인증 세션(2026-07-31 62종목 22.72조). **KRX ETF
엔드포인트는 휴장일에 직전 영업일 데이터를 그대로 준다** → 조회 전에 영업일로 스냅(안 하면 날짜
라벨이 거짓).

**게이지 본체는 §46 에서 기각 — 배선하지 않는다.** (연속 스케일로 설계한 이유: 기존 레짐 오버레이의
이진 스위치가 2026-07 에 늦게 끄고 7-31 반등을 놓치는 최악 특성을 보였다.)

**반대매매 비중 단독 사용 금지** — 평온/스트레스는 가르지만 낙폭 규모는 못 가른다:

| 국면 | 평균 | 최대 |
|---|---|---|
| 2008 금융위기 | 9.7 | 23.0 |
| **2022-06 긴축** | **8.7** | **13.1** |
| 2026-07 폭락 | — | 10.5 |
| 2020 코로나 | 6.1 | 8.5 |
| 2026-01 평온 | 1.0 | 1.7 |

2026-06-09 에 10.5 를 찍은 뒤에도 지수는 6-22 사상 최고까지 올랐다.

## 46. 취약성 게이지 오탐률 사전 측정 — 측정 완료, **게이지 기각** ⚠️

**결론: `exposure_scale` 을 노출 제어에 배선하지 않는다.** `metrics/vulnerability.py`·
`validate_vulnerability_gauge.py` 는 관측·재현용으로만 존치(docstring 에 기각 명시).

**절차**: 임계(z 1.0/2.0, 증가율 10%/20%, 반대매매 5%/8%)를 측정 **전에** 원칙만으로 고정 →
2006~2026 5,094 거래일 롤링. 경보=점수≥50, 적중=60거래일 내 −15%, 연속 경보는 에피소드로 묶음.

**3중 실패**:
1. **오탐 73%**(15건 중 11건), 경보일이 전체의 19.2%.
2. **주요 폭락 4건 전부 놓침**(2008-10 25.0 / 2011-08 0.0 / 2020-03 10.8 / 2022-06 20.0). 포착한 건
   **설계 계기였던 2026-07(54.6)** 뿐 — 단일 사건 과적합.
3. **기회비용 80.8%p**. 최악 2025-06~2026-06(207일) 경보 중 KOSPI +187.3%(이후 −38.6% 로 '적중'
   판정이지만 손해).

**임계 재조정으로 살리지 않았다** — 그게 이 절차가 막으려던 사후 확증편향이다.

**교훈**: "폭락 직후 그 사건을 설명하는 지표를 만들면 그 사건만 맞힌다." 사전 지표는 **사건 이전에
독립적으로 존재하던 근거**에서 출발하고, 판정에 **기회비용**을 반드시 포함한다. 수집 계층(§45)은
관측값·다른 설계의 입력으로 유효.

## 47. id=23의 2026-07 구간 4-arm 검증 — **스크립트 완성, 실행 보류** ⏸

VKOSPI 96.94(2009년 집계 이후 최고)·사상 첫 이틀 연속 서킷브레이커·7-13 −8.95% 구간이라 오버레이 동작을
확인하기 좋은 자연 실험 구간이다. 스크립트 `validate_id23_crash_2026.py`. **현재 상태: 3차 시도 결론 보류 — KOSPI 종가/MA200 비율
직접 대조부터 재개.**

**전제 정정**: "P2 패닉 오버레이 자연실험"으로 세웠으나 **id=23 config 에 `panic_overlay` 가 없다**
(`cadence=quarterly, regime=True, panic=False`) → '패닉 off' arm 은 현행과 동일. 4-arm 은 **레짐
필터** 검증으로만 유효. P2 검증엔 `panic_overlay` 를 켠 별도 arm 이 필요(실제 배선 공백).

**설계**: PIT KOSPI200, 4-arm(현행 / 패닉 off / 레짐+패닉 off / B&H), 종료일 07-30·07-31 병기,
up/down-beta 분리, `regime_exit`·`panic_confirm` 이벤트 일자가 직접 증거.

**판정 주의**:
- 표본 ≈22거래일·일간 변동성 6%대 → alpha 표준오차 > 추정치. **기술통계로만 보고.**
- 지수 −22% 구간에서 β0.6 의 excess (+)는 알파가 아니라 베타 부족의 산술 부산물. alpha/Sharpe 로.
- **반증 조건**: (현행 − 레짐off) 차이의 50% 이상이 7-31 하루에서 나오면 어느 결론도 채택 불가.
- 비용·체결이 결과를 지배: 슬리피지 5→25→50bps 스윕, CB 5일 거래금지 시나리오, 익일종가/시가/VWAP
  교차(7-30 신호 → 7-31 종가 체결은 +17.91% 무상 취득 구조), 7-31 상한가 3종목(§4 결론의 예외 가능).

**시도 이력**:
- **1차**: KRX 로그인 차단(§44-1)으로 PIT 0종목 — 수치 전부 폐기.
- **2차(2026-08-05) 폐기**: PIT 는 정상(union 221종목)이었으나 pykrx 로그인 경로만 막혀 펀더멘털·지수
  OHLCV 전량 실패. 3개 arm 지표가 **바이트 단위 동일**(ret −24.2%, β0.31, Sharpe −2.91, 오버레이 미발동).
  "반증 조건 충족" 출력은 gap=0 의 **0/0 인공물**. → `metrics/fetch.py` 의 `except Exception → 빈
  프레임`이 §48 범위 밖에 남아 있음이 드러나 §49 로 막았다.
- **3차(2026-08-16) 결론 보류**: 완주했으나 수치가 **2차와 소수점까지 동일**. (a) 참값인지 (b) 레짐/패닉
  파이프라인에 남은 결손인지 미결 — 워밍업 구간 `compute_panic_series` 가 pykrx 자체 버그
  (`get_nearest_business_day_in_a_week` `IndexError`)로 반복 실패. 대조 중 원시 재로그인을 짧은 간격으로
  날려 **자초한 쿨다운**으로 다시 막혔다(앱 경유 로그인은 성공했었음).

**전제**: 2026-06·07 OHLCV 와 정기변경 반영 PIT 구성의 DB 적재 확인이 선행.

## 48. 외부 데이터 소스의 조용한 실패 — 해소 ✅

§44-1 의 근본 구조. `krx_index`·`opendart`·`kofia` 가 실패 시 빈 값을 반환(49곳) — 문제는 **실패한
빈 값과 정상 빈 값이 같은 값**이라는 것. `opendart._get` 은 미설정·네트워크·에러 status·무자료가
전부 `None` 이라, 일일 20,000건 한도 초과(`020`)가 무자료(`013`)와 섞여 **한도를 소진하면 전 종목이
조용히 '재무 없음'**이 됐다.

결정(설계: `docs/superpowers/specs/2026-08-04-external-api-silent-failure-design.md`):
- **경계 셋**: 실패(raise) / 데이터 없음(정상 빈 값) / 미설정(통과). 코드로 판별 가능 — KRX 차단은
  HTML 이라 파싱 예외, 진짜 휴장일은 정상 JSON + 빈 `output`.
- **소스가 아니라 원인으로 분류**(호출자 관심사는 "재시도할까, 사람이 고칠까"): `SourceAuth/Quota/
  Unavailable/Schema/RequestError`, 소스는 속성. 쿨다운: 인증·한도 300초, 일시 장애 60초,
  스키마·요청은 없음(기다려도 안 풀리므로 대기가 문제를 감춘다).
- **"성공"은 응답 수신이지 데이터 획득이 아니다.** 과거 구간엔 DART 미공시가 많아 무자료를 실패로 세면
  정상 백테스트가 죽는다 → 집계는 **전량 실패일 때만** raise.
- **`errors.stop_aggregate`**: 쿨다운 중이면 자기유발 차단이 대표 원인을 오염시키고(Unavailable →
  Quota), 스키마 오류는 종목별로 다를 수 없으니 즉시 멈춘다(안 그러면 200종목×3회 한도 소진).
  한 번이라도 성공했으면 계속 돌려 부분 결과를 지킨다.
- **HTTP 에서 다시 뭉개지 않는다**: 전부 503 이면 온콜이 "외부 장애, 대기"로 오판해 핫픽스를 미룬다.
  Request→500, Schema→502, 나머지 503.
- **미설정은 실패가 아니다** → 용도별 preflight(`require_krx_auth()`)는 결과가 무의미해지는 진입점
  (PIT 유니버스)에만. 라이브 매매 데몬엔 걸지 않는다.
- **저하는 유지, 은폐만 제거**: `except Exception` → `except DataSourceError` 로 좁혀 **우리 버그
  (`TypeError` 등)는 전파**, 로그는 ERROR.

작업 중 발견: 라이브 `_get_sector_map` bare 호출로 섹터맵 실패 시 리밸런싱 틱 전체가 죽었다(백테스트와
저하 계약 불일치). 스크리너에 `financial_filter_applied` 신설. **테스트가 매 실행 실제 KRX 에
로그인하고 있었다**(pykrx 가 임포트 시 로그인하는 전역 부작용) — conftest 가 자격증명을 비운다(44초→25초).
661 → 770 passed.

**남은 한계**: ERROR 가 반복 경로에서 같은 사건을 여러 줄 찍는다. **억제 장치는 넣지 않았다** — 로그
핸들러·Sentry 가 없고 알림은 명시적 `publish_alert` 뿐이라 ERROR 볼륨을 소비하는 자동화가 없다. 로그
기반 알림 도입 시 함께 설계할 것.

## §49 확정 과거 데이터의 로컬 영구 저장 (완료: 2026-08-06)

백테스트 입력 6종(펀더멘털·시총·기간등락률/순매수·지수 및 전종목 OHLCV·PIT 구성·DART 재무)을 정규화
테이블 + 페치 원장(`external_fetches`)에 영구 저장, 조회는 `cached_frame` 단일 진입점. 설계:
`docs/superpowers/specs/2026-08-06-local-persistent-store-design.md`. **핵심은 원장** — 테이블만으로는
"휴장일 0행"과 "미적재"가 같은 값이라 §48 의 조용한 실패가 재현된다. `_fetch_per_market` 의
`except Exception → 빈 프레임`도 여기서 사라졌다. 미적재 구간은 pykrx 차단 중 `DataSourceError` 로
멈춘다 — 의도한 동작.

- **B1(2026-08-08) — 저장소는 빈 상태에서 시작할 것**: `stock_daily_snapshots`/`stock_period_stats`
  PK 에 시장 구분이 없어 KOSPI·KOSDAQ 이 한 키공간에 섞인다. 초기 구현은 읽기에 시장 필터가 없어
  2회차(로컬 히트)부터 전 시장이 섞였다. 이전 스키마로 적재된 행이 있다면 두 테이블 + 원장 행을 지우고
  재적재(부분 마이그레이션 스크립트 없음 — 당시 6테이블 전부 0행).
- **호출자 저하 계약**: 백테스트·리밸런싱·조회 라우트는 전파, 보조 지표만 항목 흡수. `_provider_with_flow`
  가 실패를 `flow=None` 으로 삼키던 진입로 제거. `rebalance_runner._is_risk_off` 는 기준지수 실패 시
  "위험선호로 간주해 주문" → **"이번 틱 무행동, 실패 기록 후 재시도"**(의도된 정책 변경).
- **I3 — 빈 결과는 소스가 명시적으로 "없다"고 선언할 때만 확정**: `cached_frame` 은 `row_count==0` 이면
  `is_final` 인자와 무관하게 `final=False`. 예외는 OpenDART 013 뿐(`dart_store` 가 자체 확정). 비용은
  휴장일 재조회지만, 잘못 굳혀 영구 0행이 되는 쪽이 비교할 수 없이 위험하다.
- **I4 — 범위 키는 정확일치만 히트**: 캐시키가 `(start, end)` 라 하루치 선적재가 넓은 범위 소비자
  (패닉≈90·섹터 252·레짐 `ma_period+10` 영업일)와 안 겹쳐 무의미했고 실패율 분모만 부풀렸다.
  **해소(2026-08-08)**: 지수 OHLCV 에 구간 커버리지(`index_ohlcv_coverage` + `frame.cached_range`,
  설계 `2026-08-08-index-ohlcv-coverage-design.md`) — 야간에 400 거래일을 확보하면 pykrx 차단 중에도
  레짐·패닉·벤치마크가 돈다. `stock_period_stats` 는 구간 자체가 값이라 원리적으로 정확일치 유지.
- **업종지수 "조용한 빈 성공"(2026-08-16 해소)**: `_fetch_index_tickers` 의 `except: return []` 로
  `compute_sectors` 가 `items=[]` 정상 200 을 냈다. 이제 `SourceUnavailableError` 를 올리고, 시장별
  실패는 건너뛰되 **전 시장 실패일 때만** 대표 예외.
- **`cached_range` 부분 응답 방어(2026-08-16)**: 소스가 일부만 응답해도 요청 전체를 커버로 기록하던
  문제. 도메인 지식은 소스 쪽에 둔다 — `market.estimated_trading_days`(달력일×5/7 − 연 15일) 대비 수신
  행이 절반 미만(`_COVERAGE_ROW_RATIO_THRESHOLD=0.5`)이면 커버리지 확정만 보류(데이터는 저장). 10일
  미만 구간은 검사 안 함. 반환 행 min/max 클램프는 달력일 vs 거래일이라 항상 과소 주장이 돼 배제.
  임계는 실측 근거 없는 넉넉한 안전마진.

---

> **2026-08-17 배치(§50~§57)**: `/code-review` 전체 스캔에서 각 수정 범위 밖으로 남긴 인접 이슈.

## 50. `RecommendMember.price` 가 결측 종가를 0 sentinel 로 숨김 — 해소 ✅

프론트가 `price > 0` 으로 판정해 당장 오표시는 없었지만, 새 소비처가 0 을 정상값으로 오인할 함정.
**해소(2026-08-19)**: `price: Optional[int] = None`, 프론트 `number | null`·`price != null`.

## 51. 실전(prod) 환경의 접수 불명 주문, 완전 자동 회수 불가 ⚠️

네트워크 예외로 `PENDING` 고착되던 주문에 `reconcile.py` 가 5분 유예 후 **잔고 교차확인** 회수를
추가했다. 단 **모의투자·매수 한정**(실전은 잔고 오귀속 위험, 매도는 잔고 감소로 판정 불가). 실전
자동화엔 브로커 "당일 주문목록 조회" 확장이 필요. 현재는 critical `order_unconfirmed` → 수동 확인.

## 52. `rebalance_weekday` 클램프 divergence (라이브만 클램프) — 해소 ✅

라이브 `is_rebalance_due` 만 0~4 로 클램프했다. 스키마가 막아 실영향은 없었지만 raw dict 경로 방어로
`_rebalance_dates` 도 클램프(2026-08-19).

## 53. `rebalance_runner._quotes()` 가 리터럴 "0" 시세 문자열을 결측으로 못 거름 — 해소 ✅

KIS 가 `"0"` 을 정상 응답으로 주면 `_live_equity` 가 자산을 과소계상(MDD 사이징 왜곡, 주문은
`price <= 0` 가드로 안전). `price <= 0` 을 결측으로 제외해 러너 전반 관례와 통일(2026-08-19).

## 54. `_has_holdings()` 가 PIT 유니버스 밖 보유를 "현금"으로 오판 — 해소 ✅

후보풀에서 빠진 종목만 보유한 전략이 "보유 전무"로 읽혀 MDD rearm·bootstrap 이 중복 진입할 수 있었다.
후보풀과 무관하게 포지션 존재를 직접 쿼리 — 백테스트 `not val` 계약과 일치(2026-08-19).

## 55. `cached_frame` 최초 조회(원격)와 재조회(로컬)의 dedup 형태 불일치 — 해소 ✅

저장 직전에만 dedup 해 최초 응답(중복 가능)과 로컬 히트의 형태가 달랐다. `_fetch_per_market` 의
`pd.concat` 직후 `~index.duplicated(keep="last")`(시장전환일 티커 중복)(2026-08-19).

## 56. `/turnaround` — `financial_filter_applied=False` 결과도 6시간 캐싱됨 — 해소 ✅

재무 필터 없이 나온 후보가 정상 결과처럼 6시간 굳었다. 캐싱 조건에 `financial_filter_applied` 추가.

## 57. `price_limit_model=True`(opt-in) 시 하한가 종목은 강제청산이라도 미체결 ⚠️

ADV 캡은 강제청산을 면제하지만 하한가는 KRX 규칙상 실제로 체결 불가라 면제할 수 없다 — 킬스위치의
"당일 전량 청산" 전제가 하한가에선 성립하지 않음을 문서화만 한다.

## 58. X-Forwarded-For 스푸핑으로 로그인 브루트포스 IP 스로틀 우회 가능 — 해소 ✅

`_client_ip` 가 XFF 첫 항목을 신뢰했는데 Caddy 는 클라이언트 헤더 뒤에 append 만 해, 임의값 회전으로
IP당 50회/15분 한도가 무력화됐다. Caddy `header_up X-Forwarded-For {remote_host}` 로 덮어쓰기 +
`_client_ip` 는 마지막 항목만. Caddy 반영은 당시 docker 미기동으로 미검증.

## 59. `SymbolSearch` append 재사용 시 타이핑 중 부분 문자열이 그대로 커밋됨 — 해소 ✅

단일종목용 컴포넌트를 종목 "추가"에 재사용해 "0"→"00"→… 부분 문자열이 universe 에 커밋됐다.
`commitOnType` prop(기본 true) — false 면 드롭다운 선택·Enter 로만 확정.

> §58~§61 은 base 가 오래돼 close 한 미머지 PR #104·#105 의 구현을 2026-08-18 main 기준으로 재적용한
> 것이다(원본의 §41·§42 번호는 충돌로 §60·§61 로 재기록).

## 60. `auth`/`engine` 라우트 테스트 커버리지 0 해소 ✅

`test_auth_routes.py`(가입·로그인·로그아웃·`get_current_user` 4분기)·`test_engine_control_routes.py`
(자격증명 체크가 상태 변경·Redis 발행보다 **먼저**인지, 타인 전략 start/stop 404 — IDOR). 버그 없음.

## 61. 체결품질 M2 노출·`LineChart` 테스트·App Router 에러 바운더리 — 해소 ✅

- M2(`m2_time`, 당일 종가 vs 익일 종가 사이 시장 이동)가 타입만 있고 버려지고 있었다 → "참고용, 별도
  등급 없음" 카드로 노출(백엔드 `grades` 에 등급이 없음을 확인).
- `LineChart.test.tsx` — NaN/Inf 필터, span=0 나눗셈 방지, `OverlayLineChart` 시리즈별 독립 정규화.
- `app/error.tsx` 신설 — 렌더 예외 시 재시도 경로.

## 62. `RecommendMember.market_cap` 도 §50과 동일한 0 sentinel 패턴 — 해소 ✅

§50 과 같은 계약 변경. **함정**: `market_cap` 정렬 키가 `None` 과 `int` 를 비교해 `TypeError` → 결측을
-1 로 맨 뒤로(2026-08-19).

## 63. null 허용 원화 포맷이 3곳에 인라인 삼항식으로 중복 — 해소 ✅

`lib/format.ts::fmtKRW(v, withUnit=true)` 신설, 3곳 교체(2026-08-19).

---

> **2026-08-31 배치(§64~§67)**: 전 스위트 통과 상태에서 08-19 이후 신규 표면 점검.

## 64. `StockMetric` 의 0 sentinel — §50·§62 와 같은 계약이 인접 경로에 잔존 ✅

`/api/metrics/stocks` 의 `price`·`market_cap`·`avg_value_20` 을 `Optional` 로. 프론트는 이미
nullable 이었다. `min_value`·`min_mcap` 필터는 **결측을 탈락**시킨다(결측은 더 엄격한 기준의 충족을
증명할 수 없다). 스크리너 `market_cap` 의 같은 자리는 **도달 불가**라 계약 대신 죽은 가드를 지우고
불변식을 테스트로 고정. `fmtSubscore`(= `fmtNum(v, 2)` 중복) 제거.

## 65. KIS 종목마스터 스냅샷 거래일이 UTC 기준 ✅

`snapshot_stock_master` 가 `date.today()`(컨테이너 UTC)를 썼다. 현재 beat 18:40 KST(=09:40 UTC)라
우연히 일치했지만 KST 00:00~09:00 으로 옮기면 에러 없이 하루 어긋난다. `_snapshot_target_date` 가
이미 문서화·방어한 함정을 새 경로만 지나쳤다. `now_kst().date()` 로 통일(2026-08-31).

## 66. 백테스트가 실행마다 다른 수치를 냈다 — set 순회 의존 ✅

`_apply_rebalance` 가 `set(targets) | set(val)` 을 순회 → `PYTHONHASHSEED` 무작위화로 주문 순서 →
`cash` 누적 순서가 실행마다 달라졌다(alpha `0.14965995885546152` vs `...135`). 1e-15 라 판정은
안 바뀌지만 **재현성과 수치 회귀 테스트가 불가능**해진다.

**해소**: `sorted()`. 회귀 가드는 **다른 `PYTHONHASHSEED` 서브프로세스 2개 대조**
(`test_rebalance_determinism.py`) — in-process 대조는 시드가 고정이라 정렬을 되돌려도 통과했다.
**실거래 `compute_rebalance_orders` 에도 같은 결함**(반환 순서 = 실제 주문 제출 순서라 자금이 빠듯하면
체결 바스켓이 달라진다) — parity 대로 양쪽 정렬.

## 67. `run_rebalance_backtest` 준비 로직 분리 ✅

588줄·중첩깊이 8(다음 최대 213줄·깊이 3). 준비 로직 3덩어리만 순수 헬퍼로: `_adv_frame`,
`_with_sector_map`(이 함수의 유일한 네트워크 I/O), `_parse_panic_overlay` → `PanicOverlayParams`
(frozen). 588 → 538줄, 단위 테스트 17건. `_pof` 가 `or` 대신 None 검사인 이유(`scale_in_confirm=0`
"0 으로 끄기")를 직접 고정.

**일별 루프·패닉 상태기계는 의도적으로 두었다** — 깊이 8 은 상태기계의 실제 형태이고 분기마다 측정
근거 주석이 있으며, 이 함수의 수치가 id=23 승격 근거다. 평탄화는 리팩토링을 가장한 재작성.

**검증**: 합성 2년·40종목, 전 오버레이 on 의 결과 dict 전체(스칼라 18개 + 곡선·거래 해시)를 전후 대조해
**완전 동일**(§66 없이는 불가능). 첫 골든에 `max_sector_pct` 가 없어 `_with_sector_map` 분기를 안 타는
구멍이 있어, 섹터 한도 on + 결정론적 업종 스텁 변형(체결 483→528건)으로 재대조.

**측정만 하고 남긴 것**: cProfile 상 75% 가 `_targets_at` 의 팩터 계산. 벡터화하면
`dropna().tail(N)`(유효 관측 N개)과 패널 `tail(N)`(N행)이 결측 종목에서 달라져 **수치가 바뀐다** —
착수하려면 골든 기준을 "동일"에서 "허용 오차"로 낮추는 별도 결정이 먼저다. `_vol_slippage_map` 의
`tail(21)` 한정은 측정 이득이 없고 합산 순서로 1e-15 흔들려 원복(미측정 최적화에 재현성을 내주지 않는다).

## 68. 워커가 DB 준비 전에 태스크를 소비해 거짓 `alert_cleanup_failed` — 해소 ✅

호스트 절전 복귀·재부팅 때 Docker 데몬은 컨테이너를 동시에 올린다(compose `depends_on` 은
`compose up` 에만 적용). DB 는 비정상 종료 복구 중인데 beat 는 밀린 태스크를 즉시 발송해, 가장 먼저
DB 를 잡는 태스크가 `CannotConnectNowError` 로 죽었다. 실측(2026-09-29·30 로그): DB 기동→접속 수락
9.1초, 그 직전 `cleanup_old_alerts` 가 실패해 알림 2건.

**해소**: `worker/celery_app.py::wait_for_db` 를 `worker_init` 시그널에 연결 — 컨슈머가 뜨기 전에
DB 접속을 최대 60초 기다린다(초과 시 포기하고 기동). 태스크별 재시도 대신 모든 태스크가 지나는 한
곳에서 막았다. `test_worker_wait_for_db.py` 2건 + 실 DB 대조(거부 포트 → False, 정상 → True).

**같은 로그에서 드러난 별개 사건(코드 결함 아님)**: 야간 스냅샷 7/7 실패와 10-01 분기 업종 스냅샷
(`snapshot_sector_map`, `SourceAuthError`) 실패는 **KRX 가 비밀번호 변경을 요구**해 로그인이 거부된
것. 운영자가 krx.co.kr 에서 변경 후 `secrets/krx_pw.txt` 갱신 필요. 분기 스냅샷은 다음 실행이
2027-01-01 이라 **수동 재실행**하지 않으면 2026-10 업종 PIT 가 빈다. §47 대기 사유도 이것.

## 69. `/api/trading/positions` 테스트 0건 + 평단가 0 sentinel — 해소 ✅

§26 라운드에서 보류한 `list_positions` 브로커 폴백 경로. `test_trading_positions.py` 4건 — 브로커가
답하면 DB 그림자를 섞지 않는다 / 자격증명 미등록·잔고 조회 실패는 DB 폴백 / `BrokerError` 외 예외는
삼키지 않는다. 테스트를 쓰다 **평단가 결측을 `0.0` 으로 내보내는** §50·§62·§64 와 같은 패턴을 발견 —
모니터 화면이 "0원" 으로 그렸다. `PositionOut.avg_price` 를 `float | None` 으로, 프론트는 `fmtKRW`.
