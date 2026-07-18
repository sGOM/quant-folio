# QuantFolio 다음 개선안

작성일: 2026-07-18(갱신) · 근거: PR #61~#66 완료 반영 후 코드베이스 재점검으로 신규 개선안
5건(§3~§7)을 발굴해 추가. 발굴 근거는 각 모듈이 스스로 문서화해 둔 "알려진 한계"와
배선만 되고 검증되지 않은 경로들. §4~§7 구현(PR #69) 직후 후속 재점검으로 §8~§11
4건을 추가 발굴했고, 같은 갱신에서 전부 구현 완료했다(부수적으로 worker 컨테이너의
`engine.*` 임포트가 전부 조용히 실패하던 인프라 버그도 §9 작업 중 발견해 함께 고쳤다).

## 완료 확인 (직전 우선순위 대비)

| 항목 | 상태 | 근거 |
|------|------|------|
| 전략 라이프사이클 가드 | ✅ | 미청산 포지션·가동 중 러너 존재 시 삭제 거부(409) + 테스트 (#53) |
| KIS 실시간 체결통보 연동 | ✅ | `engine/fill_notice.py` H0STCNI0/9 구독, ORGNO 저장(0009), 델타 멱등 반영 + 테스트 (#54) |
| 전략별 리스크 분리 후속 | ✅ | 전략별 일일 손실 한도 하드 게이트(계좌 한도와 AND), 비귀속 귀속 규칙 명문화 (#55) |
| 슬리피지 캘리브레이션 UI | ✅ | fill-quality 응답에 제안 필드 + 모니터 페이지 승인 UI (#56) |
| 프론트 테스트 확대(유틸) | ✅ | `lib/strategy`·`lib/api` 유닛테스트 (#57) |
| 운영 점검·정리 | ✅ | 실전 전환 게이트·0008 백필 감사 절차 문서화, 잔존물 정리·스킬 편입 (#58) |
| 보안·정확성 하드닝 | ✅ | 로그인 브루트포스 지연, 리스크 합산·체결 신뢰성, 리밸런싱 러너 루프 블로킹, 프론트 훅 일관성, StrategyForm 모듈 분해 (#59) |

또한 종전 "리스크 레이어 보류 항목"으로 남아 있던 두 건은 **이미 구현 완료**로 확인되어
본 문서에서 제거한다(코드 재점검 결과):

| 항목 | 상태 | 근거 |
|------|------|------|
| 섹터 집중 한도 | ✅ | KRX MDC 업종분류 기반 섹터 맵 + `max_sector_pct` 캡, 백테스트·라이브 러너 배선, 전략 폼 입력 필드 (#37, #39) |
| MDD 킬스위치 실거래 배선 | ✅ | `rebalance_runner._evaluate_mdd_kill` — HWM 추적·발동 시 전량 청산·`mdd_rearm_days` 쿨다운 재가동, `mdd_kill` 알림 발행 (#33) |

이번 갱신(2026-07-18)에서 아래 1건이 추가로 완료됐다:

| 항목 | 상태 | 근거 |
|------|------|------|
| DB 백업 자동화 (구 §6) | ✅ | worker 컨테이너 내 Celery beat 야간(03:00 KST) `pg_dump\|gzip` 백업(named volume `db_backups`), 실패 시 `publish_alert` critical(텔레그램), 성공 시각 Redis(`backup:last_success_at`) 기록, 보존일수(14일) 자동 정리 — 호스트 crontab 의존 제거 |

이전 갱신(2026-07-17)에서 신규 제안 2건 포함 5건이 완료됐다:

| 항목 | 상태 | 근거 |
|------|------|------|
| 데이터 계층 확장(OpenDART TTM) | ✅ | 분기 TTM(트레일링 4분기) 계산 경로 추가, `use_ttm`/`financial_period` 옵트인(기본은 기존 연간 유지 — id=23/24 재현성 보존) (#61) |
| 모의투자 실측↔백테스트 괴리 추적 (백엔드) | ✅ | `GET /api/strategies/{id}/tracking` — 체결 기반 일별 NAV 재구성, 100 정규화 오버레이, 트래킹에러·누적괴리율 (#62) |
| 섹터 맵 PIT 스냅샷 적재 | ✅ | `sector_map_snapshots` 테이블 + 분기 1회 Celery 적재 + `sector_map(as_of=...)` PIT 조회 배선(스냅샷 도입 이전 구간은 문서화된 한계로 근사 폴백 유지) (#63) |
| 모의투자 실측↔백테스트 괴리 추적 (프론트) | ✅ | 전략 상세 페이지에 실측 vs 백테스트 오버레이 차트(`OverlayLineChart`) + 트래킹에러/누적괴리율 카드 (#64) |
| E2E 스모크 자동화 | ✅ | Playwright 스모크(`frontend/e2e/smoke.spec.ts`) — 로그인~백테스트~모니터 전 구간, 야간 크론 + 수동 트리거 워크플로(`e2e-smoke.yml`) (#65) |
| 프론트 테스트 확대(훅·컴포넌트) | ✅ | TanStack Query 훅(`useSymbolNames`)·WebSocket 재연결 훅(`useWebSocket`)·`TradeLogTable`·`DsrGradeBadge` 렌더 테스트, 컴포넌트 추출 리팩터링 포함 (#66) |

---

## 1. fill_notice 실계정 검증 (실전 전환 전 필수)

`engine/fill_notice.py` 는 **실계정으로 검증되지 않은 설계 가정 3개**를 명시적으로 안고 있다
(모듈 docstring 참고):

- CNTG_QTY 를 "누적 체결수량"으로 간주한 델타 반영 — 증분수량으로 밝혀지면 로직 수정 필요.
- 체결통보 프레임에 ORGNO 미노출 가정 — ODNO 단독 매칭.
- `_parse_fill_notice` 필드 인덱스가 공식 샘플 순서 기준(종단 미검증).

→ `docs/live-order-guide.md` §2-1-A 실계정 전환 체크리스트로 검증한다. 실전(prod) 전환의
마지막 관문. **실계정 접근이 필요해 코드 작업으로는 해소 불가 — 운영 시점 수동 검증 대상.**

## 2. 마이그레이션 0008 백필 감사 (id=23+24 병행 운용 전 필수)

절차는 `docs/live-order-guide.md`에 문서화 완료. **실DB 실행·감사는 운영 시점 작업**:
`alembic upgrade head` 후 positions의 `strategy_id` NULL 잔존 행을 뽑아 수동 귀속 여부 판단.
**운영 DB 접근이 필요해 코드 작업으로는 해소 불가.**

---

## 3. TTM 재무 경로 실전 검증 (id=23/24 A/B 백테스트)

PR #61로 분기 TTM(트레일링 4분기) 계산 경로가 `opendart.metrics_by_symbol(use_ttm=...)`에
**옵트인으로 배선만 된 상태**다 — 기본값은 기존 연간 경로(id=23/24 재현성 보존)이고,
TTM이 실제로 성과를 개선하는지는 **한 번도 판정되지 않았다**.

- PIT(생존편향 제거) KOSPI200 유니버스에서 id=23(균형 멀티팩터)·id=24(밸류·퀄리티
  컨트래리언)를 연간 vs TTM 재무 A/B로 재백테스트.
- 판정 기준은 방어형 규약대로 **alpha/Sharpe** (excess/IR 금지 — 저베타 구간변경
  아티팩트에 취약).
- 개선이 확인되면 해당 전략 config의 `use_ttm` 승격, 아니면 옵트인 유지로 종결하고
  결과를 `docs/strategies.md`에 기록.

코드 변경은 사실상 없고 검증 리서치가 본체. 재무 반영 시차가 분기 단위로 짧아지는 만큼
퀄리티·성장 팩터의 신선도가 오르리라는 가설을 실측으로 확정한다.

## 4. 백테스트 체결 모델 정밀화 — 상하한가·호가단위 ✅

`app/services/backtest/portfolio.py`(§4). 옵트인 `price_limit_model`(기본 False, 재현성
보존) — 켜면 체결가를 KRX 가격대별 호가단위로 라운딩하고, 전일종가 대비 ±30% 상하한가에
도달한 방향(매수=상한가, 매도=하한가)의 주문을 그날 체결 불가로 막아 다음 리밸런싱으로
이월한다. `_krx_tick_size`/`_round_to_tick`/`_price_limit_band` 헬퍼 + `_apply_rebalance`의
`prev_prices`(직전 거래일 종가, next_close 체결도 정확히 '체결일 전날' 기준) 인자로 구현.
`RebalanceConfig.price_limit_model` 스키마 필드 추가. id=23/24 A/B 재검증(영향도 판정)은
아직 미실시 — 코드는 완료, 리서치는 후속(남은 과제 참고).

## 5. 트래킹 NAV 재구성의 사전 포지션 반영 ✅

`app/services/backtest/tracking.py`·`app/api/routes/tracking.py`(§5). 원래 제안한
"positions 테이블 스냅샷 주입" 대신, executions 로그만으로 완결되는 더 단순한 방식으로
해소했다: `reconstruct_realized_curve`가 **항상 그 전략의 진짜 첫 체결부터 전체 재생**해
현금·보유수량을 정확히 누적하고, 신규 `display_from` 인자로 **반환 곡선만** 조회 창
이후로 잘라낸다. 라우트는 `date_from`으로 주문 조회 자체를 제한하지 않고(전체 이력 조회),
정규화 단계에서 `window_execs`(표시용)와 `all_execs`(재생용)를 분리해 재생엔 항상 후자를
쓴다. Position 테이블 의존이 없어 0008 백필 감사(§2) 선행 없이도 정확 — 실행 기반 재구성이
포지션 스냅샷보다 근본적으로 신뢰 소스에 가깝다는 판단. 조회 창 이전 체결이 있었으면
응답 `notes`에 건수를 명시한다.

## 7. 실체결 기준 회전율·거래대금 보조 노출 ✅

`app/services/backtest/portfolio.py`(§7). `_apply_rebalance`가 3-튜플
`(cash, turnover, executed_notional)`을 반환하도록 확장해, ADV 캡·정수주 절사·상하한가
체결불가(§4) 반영 이후 실체결 거래대금을 별도 집계한다. 결과에 신규 필드
`avg_turnover_actual` 추가(항상 `avg_turnover` 이하). 기존 `avg_turnover`·거래 로그
필드 의미는 그대로(하위호환) — 개별 거래 로그(`trades[].amount`)는 애초부터 절사 반영
실체결액이었다는 점도 이번에 docstring으로 명문화했다.

---

## 신규 발굴 (2026-07-18 재점검, §8~§11)

발굴 근거: §4~§7 구현 직후 후속 점검 — 새로 배선된 백엔드 경로의 소비자 부재
(프론트 미노출·Redis 키 미소비)와 각 모듈이 스스로 문서화한 한계 중 코드로 해소
가능해진 것들.

## 8. 신규 백테스트 옵션·지표의 프론트 노출 ✅

`frontend/lib/api.ts`(`RebalanceConfig.price_limit_model`/`financial_period`,
`BacktestResult.avg_turnover_actual`) · `components/StrategyForm.tsx`(체결·현실성
fieldset에 상하한가·호가단위 토글) · `components/strategy-form/RebalanceFields.tsx`
(score 방식 섹션에 재무데이터 반영 주기 select) · `app/strategies/[id]/page.tsx`(자산곡선
캡션에 실체결 회전율 병기)로 노출했다.

착수 중 `use_ttm`이 **어디에도 배선돼 있지 않다**는 사실을 발견했다(§3 원문의 "옵트인으로
배선만 된 상태" 서술은 부정확했음 — `opendart.metrics_by_symbol(use_ttm=...)`는 테스트
외 호출자가 전무했다). 그래서 §8 범위를 넓혀 진짜 배선까지 했다: `RebalanceConfig`에
`financial_period: "annual"|"ttm"` 필드 추가 → `app/services/metrics/factors.py::
compute_universe_scores`(라이브·백테스트 공통 진입점)에 파라미터 스레딩 →
`engine/rebalance_runner.py`(라이브)·`app/api/routes/backtests.py::_fundamentals_provider`
(백테스트, `functools.partial`로 클로저 바인딩) 양쪽에서 `config.financial_period`를
반영. 이제 프론트 토글이 실제로 TTM 경로를 켠다 — §3(TTM A/B 재검증)을 raw config 편집
없이 UI로 수행할 수 있게 됐다.

## 9. DB 백업 신선도 감시 (last_success_at 소비자 부재) ✅

`backend/worker/tasks.py::check_backup_freshness`(신규 Celery 태스크, beat 09:00 KST) —
`backup:last_success_at`(공유 상수 `app.core.channels.BACKUP_LAST_SUCCESS_KEY`로
worker·web이 동일 키 참조)이 없거나 26시간 초과면 `publish_alert` critical
(`db_backup_stale`) 발행. `GET /api/engine/status`에 `backup_last_success_at` 필드도
추가해 운영 가시성을 넓혔다.

이 작업 중 **worker 컨테이너의 모든 `engine.*` 임포트가 조용히 실패해 왔다는 걸 발견**해
함께 고쳤다: `docker-compose.yml`의 worker 커맨드가 `celery ...`(설치된 콘솔 스크립트를
직접 실행)였는데, 이 경로로 실행하면 `sys.path[0]`이 스크립트 디렉터리(`/usr/local/bin`)가
되어 `WORKDIR`(`/app`)의 최상위 패키지(`engine`)를 찾지 못한다(`app.*`는 Celery가 `-A`
앱 모듈을 로드할 때 cwd를 임시로 꽂아줘서 우연히 살아있었다). 즉 `check_fill_quality_drift`의
알림 발행도, 이번에 새로 만든 `backup_database`의 실패 알림·`check_backup_freshness`
자체도 실제로는 한 번도 성공한 적이 없었다(§6 완료 확인 당시의 e2e 검증은 재빌드 직후
1회성 수동 호출이라 이 경로를 타지 않았던 것으로 보인다). `command: python -m celery ...`로
바꿔 해결 — `-m` 실행은 항상 cwd를 `sys.path[0]`에 넣는다. 재빌드 후 `backup_database`·
`check_fill_quality_drift`·`check_backup_freshness` 세 태스크 모두 실제 실행으로
재검증했다.

## 10. 백업 오프사이트 복제 ✅

`backend/worker/tasks.py::_upload_backup_to_s3` — 로컬 pg_dump 성공 직후 S3 호환
스토리지(AWS S3/R2/B2/MinIO, `boto3`)에 추가 업로드하는 opt-in 경로. 설정
(`app/core/config.py`: `S3_BACKUP_BUCKET`/`S3_BACKUP_ENDPOINT_URL`/`S3_BACKUP_REGION`/
`S3_BACKUP_PREFIX` + 시크릿 파일 `S3_BACKUP_ACCESS_KEY_ID`/`S3_BACKUP_SECRET_ACCESS_KEY`)
중 버킷·자격증명이 하나라도 비어 있으면(`settings.has_s3_backup`) 업로드를 통째로
건너뛴다 — 기존 로컬 전용 백업 동작에 영향 없음(현재 이 저장소는 자격증명 미설정이라
비활성 상태로 남아 있다, 계정 준비는 운영자 몫). 업로드 실패는 로컬 백업 성공을
무효화하지 않고 warning 알림(`db_backup_s3_upload_failed`)만 발행한다.
`docs/db-backup.md`·`.env.example`·`secrets/README.md`에 설정 절차를 문서화했다.

## 11. 라이브 자산가치 근사에 실현손익 반영 ✅

`engine/rebalance_runner.py::_live_equity` — 구 근사(`배정자본 + 보유 종목 미실현손익`,
확정 실현손익 무시)를 `app/services/backtest/tracking.py::replay_cash_balance`(§5 재생
로직에서 파생한 경량 버전, 일별 곡선 없이 최종 현금잔고만 계산) 기반으로 교체했다.
전략의 전체 체결(Order+Execution, 상태 PARTIAL/FILLED) 이력을 재생해 현재 현금잔고를
구하고, 여기에 보유 종목 시가평가(Position 현재 수량×현재가, 시세 조회 실패 시 평단가
폴백)를 더해 자산가치를 산출한다 — 매도 체결의 현금흐름에 매수원가와의 차익이 자연히
반영되므로 과거 라운드트립의 실현손익이 누락 없이 잡힌다. MDD 킬스위치·변동성 타겟팅의
입력 정확도가 개선됐다(손실 라운드트립 이후 자산가치가 낙관적으로 리셋되던 과소 발동
편향 해소).

---

## 신규 발굴 (2026-07-18 추가 재점검, §12~§16)

발굴 근거: §8~§11 완료 직후 코드베이스 재점검. 백엔드는 "배선만 되고 소비자가 없는
경로"(알림 미발행·API 응답에 안 실리는 문서화된 caveat) 중심으로, 프론트는 백엔드
스키마 대조로 "타입은 있는데 렌더링이 없는 필드" 중심으로 훑었다. 검증 과정에서
초기 후보 중 3건은 오탐으로 확인돼 제외했다(vts 체결이 fill-quality 표본에 자연히
누적돼 live_gate 콜드스타트 순환의존은 실제로는 없음, `v_low_confidence`·
`tracking.notes`는 이미 API 응답에 실려 있어 미노출 아님, `fill_notice.py`의
`CancelledError`/`TimeoutError pass`는 정상적인 asyncio 취소·폴링 관용구).

## 12. 야간 일봉 적재 실패 무알림 ✅

`worker/tasks.py::ingest_daily_ohlcv`(Celery beat 야간 배치) — 실패 종목 비율이
유니버스의 10%(`_INGEST_FAILURE_ALERT_RATIO`)를 초과하면 `publish_alert`로 warning
알림(`code="ohlcv_ingest_failure_rate"`, 실패 종목 일부를 메시지에 나열)을 발행하도록
확장했다. `backup_database`·`check_backup_freshness`와 같은 패턴(`user_id=None,
strategy_id=0` sentinel — 전략 무관 배치 알림)을 따랐다. `app/core/channels.py`의
알림 코드 예시 목록에도 추가.

## 13. 패닉셀 지표의 문서화된 한계가 API/UI에 미노출 (백엔드 완료, 프론트 별도)

`app/schemas/metrics.py`의 `PanicMarket`·`PanicOut`에 `caveats: list[str]` 필드를
추가하고, `app/services/metrics/panic.py`에 모듈 docstring "## 한계" 절을 그대로 옮긴
`CAVEATS` 상수(종가 확정 후 판정·장중 V자 미탐지, 브레드스 생존편향, 매매신호 아님,
S9 브레드스 미계산 4개 문구)를 정의해 `compute_panic`이 반환하는 `PanicOut`과 각
`PanicMarket` 양쪽에 채워 넣는다. `/api/metrics/panic` 라우트는 `compute_panic`
결과를 그대로 직렬화/캐시하므로 별도 배선 없이 응답에 실린다. S9 자체는 계산에
추가하지 않았다(별도 과제로 유지). 프론트 고지 배너 노출은 별도 작업.

## 14. 종목 지표 서브스코어(가치·모멘텀·저변동성) 미노출

`StockMetric.score_value`/`score_momentum`/`score_lowvol`(`app/schemas/metrics.py`)이
백엔드에서 계산·응답까지 되고 `frontend/lib/api.ts`에 타입도 있으나,
`frontend/app/metrics/page.tsx`의 `ScoreCell`은 합성 `score`만 렌더링해 서브스코어
분해가 화면 어디에도 안 뜬다. 사용자가 "왜 이 점수인지" 근거를 볼 수 없어 스크리닝
신뢰도에 영향. → `ScoreCell`에 호버/확장 시 서브스코어 분해 표시 추가.

## 15. 턴어라운드 스크리너 `smallcap_pct` 조정 불가

`/api/screener/turnaround`(`app/api/routes/screener.py`)는 `smallcap_pct`(소형주
판정 시총 하위 %, 기본 0.20)를 받고 `frontend/lib/api.ts`에도 파라미터가 정의돼
있지만, `frontend/app/screener/page.tsx`는 `surge`/`maxDebt`만 입력 필드로 노출하고
`smallcap_pct`는 UI 컨트롤이 없어 항상 기본값 고정이다("시총 하위 20%" 문구도 고정
텍스트). → 다른 두 파라미터와 같은 방식으로 슬라이더/입력 필드 추가.

## 16. 모니터 페이지 오류 메시지·엔진 이벤트 로그 세분화 부족

`frontend/app/screener/page.tsx`는 조회 실패 시 백엔드가 반환하는 실제 오류 사유(예:
OpenDART 재무 조회 실패)를 버리고 "스크리너 조회에 실패했습니다"로 뭉뚱그린다 —
`FillQualityPanel` 등 세부 오류를 보여주는 다른 화면과 일관성이 떨어진다. 또
`frontend/app/monitor/page.tsx`의 WS `execution`/`order` 이벤트 로그는
`side`/`symbol`/`qty`/`price`/`status`만 뽑아 텍스트 로그로 남기고 나머지 페이로드
필드는 구독만 하고 버린다 — 상세 진단이 필요한 사용자에게는 정보 부족. → 두 화면
모두 백엔드 오류/페이로드 원문을 (접기 가능한 형태로) 노출.

---

## 남은 과제

| 순위 | 항목 | 이유 |
|------|------|------|
| 1 | fill_notice 실계정 검증 (§1) | 실전 전환의 마지막 관문 — 미검증 가정 3개 해소. 실계정 필요 |
| 2 | 0008 백필 감사 (§2) | id=23+24 병행 운용의 남은 전제(§5 해소로 긴급도는 낮아짐). 운영 DB 필요 |
| 3 | TTM A/B 재검증 (§3) | §8에서 `financial_period` 실배선이 끝나 UI 토글만으로 수행 가능해짐 — 코드 리스크 없이 전략 개선 여지 확인 |
| 4 | 체결 모델 정밀화 id=23/24 재검증 | §4 코드는 완료 — `price_limit_model=True` 로 영향도 판정(성과 변화 미미하면 근사 유지로 종결) |
| 5 | S3 오프사이트 백업 자격증명 발급·활성화 (§10) | 코드는 완료, 버킷·키 준비는 운영자 몫(외부 계정 필요) |
| 6 | 패닉셀 caveat 프론트 노출 (§13) | 백엔드 `caveats` 필드 배선 완료 — 프론트 고지 배너/툴팁만 남음 |
| 7 | 스크리너/모니터 UI 세분화 (§14~§16) | 프론트 단독 작업, 백엔드 리스크 없음 — 우선순위는 사용 빈도에 따라 조정 가능 |

새로운 개선 후보가 쌓이면 이 문서에 이어서 추가한다.
