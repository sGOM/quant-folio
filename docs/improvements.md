# QuantFolio 다음 개선안

> 장기 방향·마일스톤은 [`docs/ROADMAP.md`](ROADMAP.md) 참고 — 이 문서는 단기 구현
> 개선안의 발굴·완료 트래킹을 담당한다.

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

## 3. TTM 재무 경로 실전 검증 (id=23/24 A/B 백테스트) ✅

`scripts/validate_ttm_ab.py`(2026-07-19) — PIT KOSPI200 에서 id=23·24 를 연간 vs TTM
재무로 반기 2-fold 워크포워드 A/B(판정은 방어형 규약대로 alpha/Sharpe). **두 전략 모두
"혼재 — 옵트인 유지"로 종결**: H1(21.1~23.6 횡보·하락장)은 TTM 우위, H2(23.7~25.6
강세장)는 연간 우위로 양 반기 일관 우위(승격 기준) 미달. id=23 은 FULL 도 연간 우위
(Sharpe 1.04/alpha +19.3%/yr vs 0.98/+17.8%), id=24 는 FULL 에서 TTM 이 근소 우위
(0.88/+12.0% vs 0.86/+11.7%, MDD 개선 −16.8% vs −18.5%)였으나 채택 근거로는 부족.
TTM 은 회전율도 +5~7%p 높아 비용 역풍. 상세 수치는 `docs/opendart-integration.md`
"분기 TTM" 절에 기록. 등록 전략 config 변경 없음(annual 유지).

## 4. 백테스트 체결 모델 정밀화 — 상하한가·호가단위 ✅

`app/services/backtest/portfolio.py`(§4). 옵트인 `price_limit_model`(기본 False, 재현성
보존) — 켜면 체결가를 KRX 가격대별 호가단위로 라운딩하고, 전일종가 대비 ±30% 상하한가에
도달한 방향(매수=상한가, 매도=하한가)의 주문을 그날 체결 불가로 막아 다음 리밸런싱으로
이월한다. `_krx_tick_size`/`_round_to_tick`/`_price_limit_band` 헬퍼 + `_apply_rebalance`의
`prev_prices`(직전 거래일 종가, next_close 체결도 정확히 '체결일 전날' 기준) 인자로 구현.
`RebalanceConfig.price_limit_model` 스키마 필드 추가.

**A/B 영향도 판정(2026-07-19, `scripts/validate_price_limit_ab.py`) — "미미, 기존 근사
유지"로 종결**: id=23·24 를 PIT KOSPI200 반기 2-fold + FULL 에서 base(False) vs
limit(True)로 비교한 결과, FULL 기준 Δalpha·ΔSharpe·Δret 모두 소수점 셋째 자리에서도
0(**|Δ| < 0.01%p**) — 임계(|Δalpha| < 1%p AND |ΔSharpe| < 0.05)를 압도적으로 하회.
KOSPI200 대형주는 호가단위가 체결가 대비 미세하고 분기 리밸런싱 종목이 상하한가에
도달하는 날이 사실상 없어(체결불가 이월 0건 — rebal 횟수·turnover_act 동일) 기존
근사(슬리피지 흡수)가 충분히 정확함이 확인됐다. 등록 전략 config 변경 없음. 소형주·
저유동성 유니버스 전략을 새로 등록할 때만 opt-in 을 재고하면 된다.

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

## 13. 패닉셀 지표의 문서화된 한계가 API/UI에 미노출 ✅

`app/schemas/metrics.py`의 `PanicMarket`·`PanicOut`에 `caveats: list[str]` 필드를
추가하고, `app/services/metrics/panic.py`에 모듈 docstring "## 한계" 절을 그대로 옮긴
`CAVEATS` 상수(종가 확정 후 판정·장중 V자 미탐지, 브레드스 생존편향, 매매신호 아님,
S9 브레드스 미계산 4개 문구)를 정의해 `compute_panic`이 반환하는 `PanicOut`과 각
`PanicMarket` 양쪽에 채워 넣는다. `/api/metrics/panic` 라우트는 `compute_panic`
결과를 그대로 직렬화/캐시하므로 별도 배선 없이 응답에 실린다. S9 자체는 계산에
추가하지 않았다(별도 과제로 유지 → §19). 프론트 고지 배너도 PR #71에서 완료 —
`frontend/app/metrics/page.tsx`가 응답 `caveats`를 접기 가능한 고지 배너로 렌더링한다.

## 14. 종목 지표 서브스코어(가치·모멘텀·저변동성) 미노출 ✅

`StockMetric.score_value`/`score_momentum`/`score_lowvol`(`app/schemas/metrics.py`)이
백엔드에서 계산·응답까지 되고 `frontend/lib/api.ts`에 타입도 있으나,
`frontend/app/metrics/page.tsx`의 `ScoreCell`은 합성 `score`만 렌더링해 서브스코어
분해가 화면 어디에도 안 뜬다. 사용자가 "왜 이 점수인지" 근거를 볼 수 없어 스크리닝
신뢰도에 영향. → PR #71에서 `ScoreCell` 툴팁으로 서브스코어 분해 표시 구현 완료.

## 15. 턴어라운드 스크리너 `smallcap_pct` 조정 불가 ✅

`/api/screener/turnaround`(`app/api/routes/screener.py`)는 `smallcap_pct`(소형주
판정 시총 하위 %, 기본 0.20)를 받고 `frontend/lib/api.ts`에도 파라미터가 정의돼
있지만, `frontend/app/screener/page.tsx`는 `surge`/`maxDebt`만 입력 필드로 노출하고
`smallcap_pct`는 UI 컨트롤이 없어 항상 기본값 고정이다("시총 하위 20%" 문구도 고정
텍스트). → PR #71에서 다른 두 파라미터와 같은 방식의 입력 필드 추가 완료.

## 16. 모니터 페이지 오류 메시지·엔진 이벤트 로그 세분화 부족 ✅

`frontend/app/screener/page.tsx`는 조회 실패 시 백엔드가 반환하는 실제 오류 사유(예:
OpenDART 재무 조회 실패)를 버리고 "스크리너 조회에 실패했습니다"로 뭉뚱그린다 —
`FillQualityPanel` 등 세부 오류를 보여주는 다른 화면과 일관성이 떨어진다. 또
`frontend/app/monitor/page.tsx`의 WS `execution`/`order` 이벤트 로그는
`side`/`symbol`/`qty`/`price`/`status`만 뽑아 텍스트 로그로 남기고 나머지 페이로드
필드는 구독만 하고 버린다 — 상세 진단이 필요한 사용자에게는 정보 부족. → PR #71에서
두 화면 모두 백엔드 오류 사유·페이로드 원문 노출(접기 가능) 구현 완료.

---

## 신규 발굴 (2026-07-18 3차 재점검, §17~§19)

발굴 근거: §12~§16 완료(PR #71·#72) 직후 재점검. 알림 발행 경로(`engine/alerts.py`)의
전달 보장을 종단까지 추적하고, 백엔드 API 응답 필드와 프론트 타입(`lib/api.ts`)을
재대조하고, 이전 배치에서 "별도 과제로 유지"라 명시해 둔 항목을 승격했다.

## 17. 알림 유실 경로 2건 — 영속화·수신 보장 부재 ✅

`app/models/models.py::Alert`(`alerts` 테이블, 마이그레이션 `0011_alerts.py`,
`user_id` nullable — NULL이면 전역/운영 알림)를 추가하고, `engine/alerts.py::
publish_alert`가 WS·텔레그램 발송과 함께 항상 이 테이블에 적재하도록 확장했다
(영속화 실패는 로그만 남기고 기존 WS·텔레그램 흐름을 막지 않음 — 하위호환).
`GET /api/alerts`(본인+전역 알림, `unread_only` 필터)·`POST /api/alerts/{id}/read`·
`POST /api/alerts/read-all`(`app/api/routes/alerts.py`)로 조회·확인 처리를 노출했다.
프론트는 기존 `AlertCenter.tsx`(WS 실시간 토스트, 우하단 고정 종 버튼)를 서버 영속화
소스로 전환 — TanStack Query로 `GET /api/alerts`를 폴링(60초, WS 이벤트 수신 시 즉시
무효화)하고, 배지는 서버 `unread_count`를, 패널 항목은 클릭 시 개별 확인 처리·"모두
읽음" 버튼으로 전체 확인 처리를 수행한다. 원안의 "헤더 벨 아이콘"은 `Nav`가 9개
페이지에 개별 임포트되는 구조라 전면 이동 대신 기존 전역 마운트(우하단 고정, 모든
보호 페이지에서 `RequireAuth` 경유 노출)를 유지했다 — 위치만 다를 뿐 알림함 기능
자체(서버 영속화·읽음 처리·배지)는 원안대로 구현. 로그인 세션으로 실제 렌더·
확인 처리 동작까지 검증 완료.

## 18. 백업 신선도의 프론트 미노출 ✅

`frontend/lib/api.ts::engineStatus` 반환 타입에 `backup_last_success_at: string | null`
을 추가하고, 모니터 페이지 엔진 상태 배지 옆에 백업 신선도 배지를 새로 노출했다
(`frontend/app/monitor/page.tsx`) — 마지막 성공 시각을 상대시간으로 표시하고,
worker의 `check_backup_freshness`(§9)와 동일 임계인 26시간을 초과하거나 성공 이력이
없으면 경고색(빨강)으로 전환한다.

## 19. 패닉셀 브레드스 S9(신저가 비율) 계산 편입 ✅

`app/services/metrics/fetch.py::_fetch_market_ohlcv_snapshot`(전 종목 단일일자 OHLCV
스냅샷, 시장당 1회 호출)을 추가하고, `app/services/metrics/panic.py::_new_low_signal`이
이를 기존 브레드스 로컬 파일 캐시 패턴(`_breadth_cache_path` 재사용, 날짜별 종가 스냅샷
누적)으로 매일 1건씩만 신규 조회해 트레일링 최대 252거래일 종가 시계열을 재구성한다.
오늘 종가가 그 종목의 트레일링 윈도우 최저가면 신저가로 집계해 비율(S9)을 산출 —
윈도우가 `_S9_MIN_WINDOW`(60거래일) 미만이면 결측 처리(초기 배포 직후 오판정 방지).
가중치는 기존 브레드스 축 30을 S5/S6/S9 각 10으로 재배분(총량 유지)했고, `CAVEATS`
문구를 "캐시 워밍업 전 결측·임계값은 잠정치" 취지로 갱신했다. 백테스트 롤링
(`compute_panic_series`)에는 연동하지 않아(실거래 대시보드 전용) 과거 백테스트
점수의 재현성에는 영향 없다. 전체 pytest(418건) 통과 확인.

---

## 신규 발굴 (2026-07-18 4차 재점검, §20~§22)

발굴 근거: §17~§19 완료(PR #74) 직후 재점검. 각 모듈이 스스로 문서화한 한계 중
**전제조건이 그 사이 해소돼 코드로 풀 수 있게 된 것**(§20), 직전 배치가 새로 만든
경로의 운영 후속 부재(§21), 방법론 docstring이 명시한 잔존 한계(§22)를 훑었다.

## 20. 팩터 섹터 중립화 (`neutralize="sector"`) — 전제조건 해소로 승격 ✅

`app/schemas/strategy.py::SelectionRule.neutralize`에 `"sector"`·`"size_sector"`를
추가했다(`Literal["none","size","sector","size_sector"]`). `app/services/metrics/
factors.py::_neutralize_sector`를 신설 — `_neutralize_size`(연속축 OLS 잔차화)와
달리 섹터는 범주형이라 섹터별 그룹 평균을 빼는 demean 방식을 쓴다(표본 1개뿐인
섹터는 자기 평균=자기 자신이라 demean 하면 정보가 소거되므로 원값 보존).
`"size_sector"`는 사이즈 잔차화 후 그 결과 위에 섹터 demean 을 순차 적용한다.

라이브 경로: `compute_universe_scores`가 `neutralize`에 `sector`/`size_sector`가
있으면 `krx_index.sector_map(as_of)`(PIT)를 조회해 `sector` 컬럼을 주입한다.
백테스트 경로: `backtests.py::_fundamentals_provider_with_market_cap`를
`_fundamentals_provider_with_neutralize_cols`로 일반화해 시총·업종 컬럼을 필요한
축만 선택적으로 붙이고, `portfolio.py::_targets_at`의 펀더멘털 병합 컬럼 목록에
`"sector"`를 추가해 스코어링 프레임까지 흘러가게 했다. 프론트(`RebalanceFields.tsx`)
select 에도 "업종 중립화"/"시가총액 + 업종 중립화" 옵션을 추가.

`_neutralize_sector`(그룹평균 제거·NaN 보존·단독섹터 보존 3건)의 유닛테스트를
`tests/test_quality_factor.py`에 추가, 전체 pytest(422건) 통과 확인.

**id=23 PIT A/B 판정(2026-07-19, `scripts/validate_sector_neutralize_ab.py`) —
"현행(neutralize 미적용) 유지"로 종결**: base vs `"sector"` vs `"size_sector"`,
반기 2-fold + FULL, 방어형 규약(alpha/Sharpe). `"sector"`는 H2·FULL에서 근소
우위(FULL Sharpe 1.07 vs 1.04, alpha +19.6% vs +19.3%)였지만 H1(횡보·하락장)에서
알파가 소멸(−0.2% vs +1.4%, Sharpe −0.11 vs 0.02)해 양 반기 우위 실패 — 혼재.
`"size_sector"`는 FULL 포함 전면 열위. 해석: id=23의 업종 쏠림은 제거해야 할
왜곡이 아니라 방어 구간 알파의 원천 일부(저변동 팩터가 특정 업종에 자연 편중)로,
demean 이 이를 깎아냈다. 등록 config 변경 없음. 한계: `sector_map_snapshots`가
아직 비어 있어 전 구간이 현재 KRX 분류 폴백(C-2)으로 돌았다 — 스냅샷이 수년치
쌓인 뒤에도 결론이 뒤집힐 가능성은 낮지만(분류 변경은 드묾) 참고.

## 21. alerts 테이블 보존정책·반복 알림 억제 부재 (§17 운영 후속) ✅

`worker/tasks.py::cleanup_old_alerts`(신규, beat 매일 04:00 KST — 백업 03:00과
겹치지 않는 시간대) — 읽음 처리 90일·미읽음 180일 초과분을 삭제한다
(`_ALERT_RETENTION_READ_DAYS`/`_ALERT_RETENTION_UNREAD_DAYS`). `celery_app.py`
beat_schedule 에 `cleanup-old-alerts` 등록.

`engine/alerts.py::publish_alert`에 옵트인 `dedup_window_hours` 파라미터를
추가했다 — 같은 `(user_id, strategy_id, code)`의 미확인(is_read=False) 알림이
창 안에 이미 있으면 DB 재적재·텔레그램 재발송을 건너뛴다(WS 토스트는 dedup 여부와
무관하게 항상 통과시켜 실시간성 유지). 상태형 배치 경보 `db_backup_stale`
(`check_backup_freshness`, 매일 1회 실행이라 20시간 창)·`ohlcv_ingest_failure_rate`
(야간 배치, 20시간 창)에 적용했다.

`GET /api/alerts`에 `offset` 쿼리 파라미터를 추가(`limit`은 기존 그대로)해 이전
이력 조회 수단을 붙였고, `frontend/lib/api.ts::listAlerts`에도 `offset` 인자를
추가했다.

프론트 연동(2026-07-19): `AlertCenter.tsx`를 `useInfiniteQuery`(offset 페이지네이션,
50건 단위)로 전환하고 목록 하단에 "이전 알림 더보기" 버튼을 붙였다. 응답에
`has_more`가 없어 마지막 페이지가 꽉 찼는지로 다음 페이지 유무를 추정하고, 새
알림 유입으로 offset이 밀릴 때의 페이지 경계 중복은 id로 걸러낸다.

## 22. DSR 동질 시행 집합의 유니버스 식별 부재 ✅

`backtests.py::_universe_fingerprint` — 백테스트 실행 시점의 실제 유니버스(PIT
해소 결과 포함, 정렬)+`universe_rule` 파라미터를 sha256 해시(16자)로 만들어
`result["universe_fingerprint"]`에 저장한다. `run_strategy_backtest`가 결과를
저장하기 직전에 채운다.

DSR 라우트(`GET /backtests/{id}/dsr`)의 동질 집합 조회를 2단계로 바꿨다: 기존과
동일하게 `strategy_id + period_start/period_end`로 1차 후보를 뽑은 뒤, 대상
백테스트에 지문이 있으면 지문까지 일치하는 행으로 좁힌다. 지문이 없는 과거
이력(§22 도입 이전 실행)은 대상 자체에 지문이 없을 때만 기존 기간 필터로
폴백한다(하위호환 — 신규 백테스트가 쌓일수록 집합 순도가 자연 개선). Backtest 테이블
스키마 변경은 불필요(`result` JSONB에 필드 추가만). `deflated_sharpe.py` 모듈
docstring·"한계" 절도 갱신해 잔존 한계(지문 없는 이력이 섞인 구간만 영향)를
명시했다.

---

## 신규 발굴 (2026-07-20 재점검, §23)

발굴 근거: §21 alerts 프론트 연동 완료 직후 재점검 — 남은 과제 4개가 전부 외부
자원(실계정·운영DB·자격증명·장기 운영이력) 필요로 코드 작업이 막혀, 코드베이스
전반(엔진 상시 루프·alerts 신규 로직·§21 방금 만든 API 응답)을 다시 훑어 코드만으로
완결 가능한 후보를 찾았다.

## 23. 엔진 상시 루프 무알림·alerts 회귀 방어 공백·has_more 추정 제거 ✅

**상시 루프 무알림**: `engine/main.py::_reconcile_loop`(미체결 주문 재조회, 60초
주기)·`_fill_notice_loop`(체결통보 구독 재동기화, 60초 주기)가 예외를 로그만 남기고
삼킨 채 다음 주기에 재시도해, 연속 실패가 며칠 지속돼도 알림이 없었다(`_reconcile_loop`가
죽으면 미체결 주문이 영원히 SUBMITTED로 남는 금전 리스크). `base_runner.py`의 러너별
연속 실패 알림 패턴(임계 도달 '순간'에만 1회 발행, 성공 시 리셋, `FAILURE_ALERT_THRESHOLD`
공유)을 이 두 전역 루프에도 이식했다. 전역 루프라 특정 사용자를 특정할 수 없어
`user_id=None, strategy_id=0`(`worker/tasks.py`의 `db_backup_failed` 등과 동일한
sentinel 관례)로 `critical` 알림을 발행한다.

**alerts 신규 로직(§17/§21) 테스트 공백**: `publish_alert`의 `dedup_window_hours`
DB dedup, `worker/tasks.py::cleanup_old_alerts`(읽음 90일/미읽음 180일 분리 삭제
보존정책), `app/api/routes/alerts.py`(목록·읽음처리·offset 페이지네이션) 모두 유닛테스트가
0건이었다. `tests/test_alerts_dedup.py`(dedup 스킵/삽입/window 미지정 3건)·
`tests/test_alerts_cleanup.py`(보존정책 분리 삭제 2건 — 실제 delete() 문의 WHERE 절을
SQLAlchemy 내부 평가기 `sqlalchemy.orm.evaluator._EvaluatorCompiler`로 정확히 평가해
재구현 오차 없이 검증)·`tests/test_alerts_route.py`(has_more 계산 3건·확인처리 소유권
4건·전체확인 1건) 신설, 총 13건 추가. 전체 pytest 460건 통과(로컬 환경엔 FinanceDataReader
미설치로 무관한 기존 2건만 격리 실패).

**`GET /api/alerts` has_more 추정 제거**: 방금 만든 §21 프론트 "더보기"가 "마지막
페이지 길이==limit"이라는 추정 휴리스틱을 썼는데, 라우트가 `limit+1`건을 조회해
초과분 존재로 `has_more`를 정확히 계산하도록 바꿨다(`AlertListOut.has_more` 필드
추가). `frontend/lib/api.ts::AlertListOut`·`AlertCenter.tsx`의 `getNextPageParam`도
추정 로직 대신 이 필드를 그대로 쓰도록 갱신.

**`reconcile_open_orders`의 사용자별 브로커 실패 무계측 후속**: `engine/reconcile.py::_broker`가
브로커 생성 실패(`BrokerError`, 예: 자격증명 만료)를 로그만 남기고 `stats`
어디에도 반영하지 않아, 특정 사용자의 자격증명이 계속 만료 상태여도
`_reconcile_loop`의 실패 카운터(위 §23 첫 항목)가 못 보는 구멍이 있었다.
`_broker`가 이제 실패 시 `stats["errors"]`를 올리고, `_reconcile_loop`는 하드
예외뿐 아니라 `stats["errors"] > 0`도 같은 임계-교차 알림 로직에 반영한다.
`tests/test_reconcile.py` 신설(브로커 생성 실패 시 errors 계측 확인 1건 + 정상
경로 대조군 1건).

---

## 신규 발굴 (2026-07-20 재점검, §24)

발굴 근거: §23(엔진 상시 루프·alerts 테스트·has_more·reconcile 계측) 완료 직후
3번째 재점검 — 이전 두 배치가 안 훑은 각도(리스크 안전장치의 프론트 노출,
Celery beat 배치 태스크의 알림 배선 공백)에서 찾았다.

## 24. MDD 킬스위치 상태 노출 + 배치 태스크 무알림 2건 해소 ✅

**MDD 킬스위치 상태 노출**: `engine.rebalance_runner`가 `rebalance:mdd:{id}`에
고점(HWM)·발동 여부·발동일을 갱신해 왔지만 어떤 API 도 읽지 않아, 발동(전량 청산)
순간의 alert 토스트/알림함 메시지 외엔 나중에 "지금 청산 상태로 재가동 대기 중"임을
확인할 방법이 없었다. Redis 키 빌더를 `app/core/channels.py::mdd_state_key`로
옮겨(기존 `engine_health_key`와 동일한 web/engine 공유 규약 위치) `rebalance_runner.py`가
이를 쓰도록 정리하고, `GET /api/engine/strategies/health` 응답에
`mdd_killed`/`mdd_kill_date`/`mdd_hwm` 필드를 추가했다. 모니터 페이지
(`StrategyHealthPanel`)에 발동 중일 때만 뜨는 "MDD 킬 발동중" 배지(warning
variant, 발동일·쿨다운 재가동 안내 툴팁)를 붙였다. `tests/test_engine_health_route.py`
신설(4건 — 발동/기본값/HWM만 추적/헬스와 독립).

**Celery beat 배치 태스크 무알림 2건**: `worker/tasks.py::snapshot_sector_map`
(분기 1회 PIT 업종 스냅샷 적재)이 실패를 예외 재전파만 하고 알림이 없어, 조용히
실패하면 최소 3개월간 §20 판정 근거였던 PIT 섹터 데이터가 스테일 상태로 남을 수
있었다. `cleanup_old_alerts`(§21 보존정책 삭제)도 동일하게 무알림이었다(영향도는
낮음 — 실패해도 alerts 테이블이 조금 더 커지는 정도). 같은 파일의 다른 배치
태스크(`ingest_daily_ohlcv`·`backup_database`·`check_backup_freshness`·
`ingest_news`)가 따르는 `publish_alert(user_id=None, strategy_id=0, ...)`
sentinel 관례를 두 곳에 이식했다 — 코드(`sector_map_outage`)는 warning,
alerts 정리 실패(`alert_cleanup_failed`)도 warning.

## 신규 발굴 (2026-07-20 재점검, §25)

발굴 근거: §24 완료 직후 4번째 재점검 — alerts/engine 루프/MDD/reconcile/has_more(§1~24)는
제외하고 새 각도(alerts 이외 화면의 백엔드 노출 공백, Celery beat 태스크, WS 이벤트 처리,
risk_layer 배선, 팩터 IC/IR 노출, KIS 연동 에러 처리, 테스트 커버리지)를 훑었다. Celery
beat 전체 태스크·WS 이벤트 타입·팩터 IC 노출·risk_layer 배선은 이미 해소되어 있었고,
실시간 시세 WS(`engine/price_feed.py`/`engine/kis_ws.py`)와 KIS REST 클라이언트에서
새 공백 3건(§25~27, 별도 PR로 순차 진행)을 찾았다.

## 25. 실시간 시세 WS 재연결 실패 무알림 해소 ✅

`PriceFeedManager._supervise`는 KIS 실시간 시세 WS 연결이 끊기면 지수 백오프
(최대 120초)로 재연결을 시도하지만, 반복 실패해도(앱키 만료, KIS 측 장애 등)
로그 경고만 남기고 알림이 없었다. 러너는 `price:{symbol}` 캐시가 없으면 REST 로
폴백해 매매 자체는 죽지 않지만, 실시간성이 조용히 저하된 채 장시간 방치될 수
있었다. `_reconcile_loop`/`_fill_notice_loop`(§23)와 동일한 임계-교차 1회 알림
패턴(`FAILURE_ALERT_THRESHOLD`)을 사용자별로 적용 — `PriceFeedManager`에
`_fail_counts: dict[user_id, int]`를 추가해 성공 시 리셋, 연속 실패가 임계치에
닿는 순간에만 `publish_alert(user_id=<해당 사용자>, code="price_feed_outage",
severity="warning", dedup_window_hours=6.0)`을 발행한다(사용자별 피드라 시스템
sentinel(`user_id=None`)이 아닌 실제 user_id 사용 — WS 토스트도 정상 전달됨).
`test_price_feed.py` 신설(4건 — 임계 교차 시 정확히 1회 알림, 임계 미달 무알림,
성공 시 카운터 리셋). 이전까지 이 모듈은 테스트 0건이었다.

## 26. `engine/kis_ws.py` 테스트 커버리지 0 해소 ✅

실시간 매매의 가격 소스인 `KisWebSocketClient`(구독 메시지 생성·PINGPONG 응답·
체결가 프레임 파싱)에 테스트가 전무했다. `test_kis_ws.py` 신설(7건 — 구독
tr_type 1/2, PINGPONG 응답, 정상 체결가 파싱과 그 예외 경로들 — 잘못된
tr_id·0가·파싱 불가 가격). 실제 WS 연결(`run`/`issue_approval_key`)은 실
네트워크가 필요해 이 스위트 범위 밖 — `_handle`처럼 네트워크 없이 검증
가능한 순수 로직만 다룬다.

## 27. KIS REST 유량제한 재시도가 시세 조회에만 있던 비대칭 해소 ✅

`app/services/kis/client.py::KisClient`는 EGW00201(유량제한) 재시도를
`get_current_price`에만 갖고 있었고, `place_order`/`get_order_execution`/
`get_balance`는 `rt_cd != "0"`이면 즉시 실패했다(§신규발굴 3차 3위 — 애초
"낮은 확신도"로 올렸으나 실제 코드를 보니 진짜 비대칭이었다). 전역
throttle(`_RateLimiter`)로도 못 막는 교차 프로세스(web·engine·worker 동시
접근) 경합에서 이 세 호출도 EGW00201 을 맞을 수 있는데, 주문·체결조회·
잔고조회 실패는 시세 조회 실패보다 파급(미체결 오판, reconcile 오탐 등)이
크다. HTTP/`rt_cd` 검증과 재시도 로직을 `_request_json` 공통 헬퍼로 추출해
네 메서드 모두 동일한 보호를 받도록 통일했다. `test_kis_client.py` 신설
(5건 — 재시도 없이 성공, 재시도 후 성공, 재시도 소진 후 실패, 비유량제한
오류는 즉시 실패, get_balance 배선 확인). 이전까지 `KisClient`의 HTTP
경로는 테스트 0건이었다.

## 신규 발굴 (2026-07-20 재점검, §28~§30)

발굴 근거: §25~27 완료 직후 4번째 재점검 — §1~27 전체(runner·MDD·PIT·팩터·체결·
슬리피지·ohlcv·백업·실전게이트·업종분류·알림정리·price_feed·kis_ws·KIS REST
재시도)는 제외하고 새 각도를 훑었다. `TossClient`의 HTTP 레벨(§28),
`recommend.py` 추천 스코어링(§29), `ws.py` 실시간 중계(§30, 낮은 확신도) —
3건을 별도 PR로 순차 진행한다.

## 28. `TossClient` HTTP 레벨(재시도·토큰 락) 테스트 커버리지 0 해소 ✅

`tests/test_broker.py`는 `TossClient._request`를 통째로 몽키패치해 대체해
왔기 때문에, `_request` 내부(429 시 Retry-After 파싱 후 1회 재시도)와
`get_access_token`의 Redis 분산락 single-flight, `_headers`의 계좌 필요
검증은 이제까지 한 번도 실행된 적이 없었다 — §27에서 고친 `KisClient`와
정확히 같은 사각지대다. 토스는 모의투자 환경이 없어 자격증명이 항상
실거래로 동작하므로(모듈 docstring), 락 경합 시 이중 토큰 발급이나 429
처리 버그가 실제 주문 실패로 직결될 수 있다. `test_toss_client.py` 신설
(7건 — 토큰 신규 발급 후 조회, 캐시 토큰 재사용, 429 재시도 후 성공, 429
재시도 소진 후 실패, 비재시도 오류 즉시 실패, 계좌 미등록 시 HTTP 호출
없이 즉시 실패, 락 경합 시 재발급 없이 락 보유자의 캐시 토큰 재사용).
검증 중 `_request`의 루프 마지막 전용 실패 메시지("429 재시도 후에도
실패")가 현재 로직상 도달 불가 코드임을 확인(두 번째 429도 `attempt==0`
조건을 벗어나 앞의 "HTTP 429" 분기로 먼저 처리됨) — 동작에는 영향 없어
수정하지 않고 테스트 주석으로만 기록.

## 29. `app/services/recommend.py` 추천 스코어링 테스트 커버리지 0 해소 ✅

추천 화면 전용 KOSPI200 멀티팩터 스코어링(`compute_kospi200_scored`)에 유닛
테스트가 전무했다. `_reindex`(인덱스 정렬·NaN 처리), 종가 폴백(시세 결측 시
시가총액/상장주식수로 대체), OpenDART 조회 실패 시 중립 처리(`except
Exception: qmetrics = {}`) 등 순수 로직 경로가 실제로 정확히 동작하는지
검증된 적이 없었다. 실패해도 매매엔 영향 없어(추천 화면 전용) §28보다
우선순위는 낮지만, 화면에 잘못된 가격·팩터점수가 노출될 수 있다는 점에서
가치가 있다. `test_recommend.py` 신설(5건 — 멤버 부재/시가총액 부재 시 빈
결과, 종가 결측 시 시가총액/상장주식수 폴백, OpenDART 예외 시 중립 처리로
예외 없이 완료, OpenDART 정상 응답 시 항목 채움과 시가총액 내림차순 정렬).
KRX/OpenDART 헬퍼는 `app.services.recommend` 네임스페이스에 바인딩된 이름을
대역화해 네트워크 없이 검증했다.

## 30. `app/api/routes/ws.py` 실시간 이벤트 중계 테스트 커버리지 0 해소 ✅

`_relay`/`_drain` 태스크 경합 종료·`pubsub.unsubscribe`/`aclose` 정리 로직에
테스트가 없었다(§신규발굴 4차 3위, 낮은 확신도). 코드 자체가 짧고 이미
방어적으로 작성돼 있어(`FIRST_COMPLETED`로 양쪽 정리, `WebSocketDisconnect`
무시) 실제 버그 발견 가능성은 낮았으나, 인증 실패 시 close(4401)·정상 중계·
연결 종료 시 pubsub 자원 정리라는 핵심 계약은 검증해 둘 가치가 있었다.
`test_ws_events.py` 신설(3건 — 미인증 연결이 4401로 닫힘, 정상 인증 시
connected 메시지 후 pubsub 이벤트 중계와 종료 시 unsubscribe/aclose 확인,
JSON 파싱 실패 메시지는 건너뛰고 다음 정상 메시지만 전달). 실 Redis 없이
`redis_client.pubsub()`·`_authenticate`를 대역화하고 `TestClient.
websocket_connect`로 검증했다. 사전 우려와 달리 실제 버그는 발견되지
않았다 — 순수 커버리지 확충 성격.

## 신규 발굴 (2026-07-21 재점검, §31)

§1~30 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색. 실거래
핵심 경로 테스트 공백 1건을 최우선으로 선정해 구현. 나머지 후보(`engine/
fills.py::record_fill`의 오버셀 무경보 클램프, `broker/factory.py`의 시세
전용 헬퍼 테스트 공백)는 확신도 중간~낮음으로 이번엔 보류.

## 31. `engine/reconcile.py` 체결 정합 핵심 로직 테스트 커버리지 0 해소 ✅

기존 `test_reconcile.py`는 브로커 생성 실패 계측(§23 후속)만 검증했고, 이
모듈이 스스로 "멱등 안전성"을 핵심 계약으로 명시한 정작 핵심 로직 —
`_process_one`의 델타(증분) 계산·전량/부분 판정, 수량은 이미 일치하는데
상태만 어긋난 경우의 보정, 모의투자 잔고 폴백(매수 한정·타 주문 소비분
차감), 주문 단위 분산 락으로 인한 중복 처리 스킵 — 은 단 1건도 실행된 적
없었다(§신규발굴 5차 1위). 여기 버그는 실제 포지션·현금 수량 오류로 직결
된다. `test_reconcile.py`에 9건 추가(전량체결 델타 반영, 부분체결 증분만
반영, 수량 일치·상태만 stale 인 경우 record_fill 미호출·상태만 보정, KIS
조회 실패 시 모의투자 매수 잔고 폴백, 잔고에 종목 없을 때 조용히 스킵, 매도
주문은 폴백 자체를 타지 않음, 실전 환경에선 폴백 비활성, 같은 종목을 다른
주문이 이미 소비한 수량은 중복 배정하지 않음, 락 보유 중인 주문은 브로커
생성조차 시도하지 않음). `_order_recorded_qty`/`_symbol_recorded_qty`/
`record_fill`은 reconcile 네임스페이스에서 대역화해 정합 자체의 분기 로직만
순수하게 검증했다. 실행 결과 실제 버그는 발견되지 않았으나(로직 자체는
정확했음), 향후 회귀를 막는 안전망을 확보했다.

## 신규 발굴 (2026-07-21 재점검, §32)

§1~31 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색(6차).
`engine/fills.py::record_fill`의 오버셀 무경보 클램프, `broker/factory.py`의
시세 전용 헬퍼 테스트 공백은 5차에 이어 확신도 중간~낮음으로 이번에도 보류.

## 32. `engine/runner.py::StrategyRunner` 단일종목 매매 핵심 로직 테스트 커버리지 0 해소 ✅

`RebalanceRunner`(리밸런싱)는 `test_engine_e2e.py`로 종단 검증돼 있었지만,
개별 종목을 손절%/익절%/트레일링·리스크 한도로 실거래 청산하는
`StrategyRunner._tick_once`는 이제까지 참조하는 테스트가 전무했다(§신규발굴
6차 1위). 손절이 매수 신호보다 우선 처리되는 순서, `RiskLimit.stop_loss_pct`
손절과 전략 config 청산(손절%/익절%/트레일링)의 독립적 작동, 트레일링 고점의
Redis 캐시 갱신·보유 종료 시 정리, `(user, symbol)` 포지션 락 경합 시 tick
스킵, 계좌 공통 일일 손실 한도 초과 시 신규 매수 진입 차단 — 자금 리스크
직결도가 가장 높은 이 계약들을 `test_strategy_runner.py` 신설(9건)로
검증했다. `test_engine_e2e.py`와 동일한 인메모리 DB(FakeDB/`_Store`)·
FakeBroker·FakeRedis로 실제 `risk.py`/`executor.py`를 그대로 태워 종단으로
확인했으며(신호 자체는 `latest_signal`만 고정값으로 대역화), 실제 버그는
발견되지 않았으나(로직은 정확했음) 향후 회귀를 막는 안전망을 확보했다.

## 신규 발굴 (2026-07-21 재점검, §33)

§1~32 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색(7차).
`engine/risk.py` 순수 유닛 테스트(경계값), `worker/tasks.py`의 백업·뉴스
수집 태스크 테스트 공백은 확신도 중간~낮음으로 이번엔 보류.

## 33. `engine/executor.py::execute_signal` 3중 멱등 방어·거부 경로 테스트 커버리지 0 해소 ✅

모든 러너(StrategyRunner·RebalanceRunner)가 공유하는 단일 주문 진입점인데,
기존 `test_executor.py`는 `make_idempotency_key`의 결정성만 검증했고, 이중
매수/매도를 막는 핵심 방어선 — 3중 멱등 방어(Redis 락 경합 시 즉시 반환,
DB `idempotency_key` 기존 주문 스킵, `IntegrityError` UNIQUE 충돌 흡수) —
와 `broker.place_order`가 `BrokerError`를 던질 때 `REJECTED` 기록·이벤트
발행 경로는 정상 체결 경로만 e2e/StrategyRunner 테스트로 간접 검증됐을 뿐
직접 검증된 적이 없었다(§신규발굴 7차 1위). `test_executor.py`에 7건
추가(전량체결 정상 경로, Redis 락 경합, DB 기존 주문 발견, IntegrityError
흡수, 증권사 주문거부 REJECTED 기록, 미체결 접수 시 SUBMITTED 유지, 체결
조회 자체 실패 시 SUBMITTED 로 남아 reconcile 에 위임). `execute_signal`은
세션을 인자로 직접 받으므로 `test_engine_e2e.py`와 동일한 FakeDB(`_Store`)·
FakeBroker·FakeRedis 를 팩토리 패치 없이 바로 사용했다. 실제 버그는
발견되지 않았으나(로직은 정확했음) 향후 회귀를 막는 안전망을 확보했다.

## 신규 발굴 (2026-07-21 재점검, §34)

§1~33 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색(8차).
`engine/fills.py::record_fill`의 오버셀 무경보 클램프, `broker/factory.py`의
시세 전용 헬퍼 테스트 공백은 5~7차에 이어 확신도 중간~낮음으로 이번에도 보류.

## 34. `engine/risk.py` evaluate_buy/evaluate_sell/check_stop_loss 순수 유닛 테스트 커버리지 0 해소 ✅

`test_per_strategy_risk.py`는 `check_daily_loss_limit`/`_daily_pnl`만 정밀
검증했고, 실제 주문 수량·청산 여부를 결정하는 `evaluate_buy`/`evaluate_sell`/
`check_stop_loss`/`_aggregate_position`은 직접 호출하는 테스트가 전무했다
(§신규발굴 8차 1위, grep 으로 재확인해 확신도 중간→높음 상향). `max_position_
size` 잔여한도 소진 시 즉시 거부, 가용 현금이 1주 미만일 때 거부, 유효하지
않은 가격 방어, `evaluate_sell`의 전략 스코프(자기 전략 보유분만) vs 계좌
합산 스코프(여러 전략 합산) 분기, `check_stop_loss`의 RiskLimit 없음/
`stop_loss_pct` 미설정/`avg_price<=0` 방어, `_aggregate_position`의 여러
전략 동일 종목 수량가중 평균 계산 — 을 `test_risk_evaluate.py` 신설(15건)로
검증했다. `test_per_strategy_risk.py`의 픽스처 패턴을 재사용하되, 이 세
함수는 db.execute(JOIN)를 쓰지 않아 conftest 기본 FakeDB 로 충분했다. 실제
버그는 발견되지 않았으나(경계 조건 전부 정확했음) 향후 회귀를 막는 안전망을
확보했다.

## 35. `engine/fills.py::record_fill` 오버셀 무경보 클램프 해소 ✅

매도 체결이 보유수량을 초과하면 `pos.qty`가 조용히 0으로 클램프되고 로그·
알림이 전혀 없었다(5~8차에 걸쳐 이월된 후보, 확신도 중간). 정합 경쟁(§31
분산 락으로 대부분 막히지만 이론상 배제 불가)이나 이중 체결 기록이 발생하면
포지션 수량이 흔적 없이 사라져 계정 상태 오류를 은폐할 수 있었다.
`record_fill`에 선택적 `redis` 매개변수를 추가해, 오버셀 감지 시 warning
로그와 함께(§21 알림 인프라 재사용) `oversell_clamped` 코드로 사용자별
warning 알림을 발행하도록 수정했다. `engine.alerts`가 이미 `engine.fills`를
import 하므로 순환 import 를 피하기 위해 함수 내부에서 지연 import 했다.
3개 호출부(`executor.execute_signal`·`reconcile._apply`·`fill_notice.
apply_fill_notice`) 모두 이미 갖고 있던 `redis`를 그대로 전달하도록
수정(하위호환: `redis=None`이면 기존처럼 로그만). `test_multi_strategy_
positions.py`에 오버셀 클램프+알림 발행 검증과 정상 매도는 알림이 없음을
확인하는 대조군 테스트 2건을 추가했다.

## 신규 발굴 (2026-07-21 재점검, §36)

§1~35 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색(9차,
worker/api routes/backtest core/frontend 로 범위 확대). 실거래 핵심 경로가
거의 소진돼 이번 라운드는 후보 강도가 이전 차수보다 낮았다. `app/api/routes/
trading.py::list_positions`의 브로커 폴백 로직(조회 전용, 자금 리스크 낮음)
은 확신도 낮음~중간으로 보류.

## 36. `worker/tasks.py` DB 백업 순수 로직(URL 파싱·보존정책) 테스트 커버리지 0 해소 ✅

`_backup_database_async`(pg_dump·S3·celery beat 배선)를 참조하는 테스트가
전무했다(§신규발굴 9차 1위). 그중 외부 I/O(subprocess/boto3) 없이 검증
가능한 순수 함수 두 개 — `_parse_database_url`(DATABASE_URL → pg_dump 접속
정보 파싱, 필드 누락 시 기본값 폴백)·`_prune_old_backups`(보존기간 계산·
파일명 패턴 매칭·삭제 대상 판정) — 를 `test_backup_tasks.py` 신설(7건)로
검증했다. 여기 버그는 "야간 백업이 조용히 잘못된 host/port 로 실패"하거나
"보존기간 계산이 틀려 백업이 너무 일찍 삭제/과다 보존"되는 방식으로 나타나
데이터 손실 리스크와 직결된다. `_run_pg_dump_gzip`/`_upload_backup_to_s3`는
실제 프로세스·네트워크 의존이라 이번 범위에서 제외했다. 실제 버그는
발견되지 않았으나(로직은 정확했음) 향후 회귀를 막는 안전망을 확보했다.

## 신규 발굴 (2026-07-21 재점검, §37)

§1~36 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색(10차,
백테스트 코어·API 라우트·프론트엔드로 범위 확대). 백테스트 코어는 이미
다수 테스트로 충분히 커버돼 있었고, 프론트엔드에서는 뚜렷한 고가치 신규
후보를 찾지 못했다. `trading.py::list_positions` 브로커 폴백(조회 전용,
자금 리스크 낮음)은 9~10차에 걸쳐 확신도 낮음~중간으로 보류 지속.

## 37. `app/api/routes/auth.py` 로그인 브루트포스 방어 로직 테스트 커버리지 0 해소 ✅

`_login_blocked`/`_record_login_failure`/`_reset_login_failures`(Redis 고정
윈도우 기반 이메일당 10회·IP당 50회 임계 429 차단, bcrypt 오프라인 방어와
별개인 온라인 무차별 대입 1차 방어선)를 참조하는 테스트가 전무했다
(§신규발굴 10차 1위 — `test_security.py`는 해싱만 검증). `test_login_
bruteforce.py` 신설(8건)로 임계 도달 경계값(9회 통과·10회째 차단), IP
카운터로 다계정 스프레이 차단, 로그인 성공 시 이메일 카운터만 리셋되고 IP
카운터는 유지되는 비대칭, Redis 장애 시 "차단 안 함"으로 안전하게 열리는
폴백(가용성 우선 설계) 을 검증했다. `conftest.FakeRedis`에 `incr`/`expire`
를 추가해 재사용 가능하게 확장했다. 실제 버그는 발견되지 않았으나(로직은
정확했음) 향후 회귀를 막는 안전망을 확보했다. 부수적으로 호스트 개발환경에
`email-validator`(requirements.txt 에는 있으나 미설치 상태였음) 를 설치해
`app.api.routes.auth` import 경로 테스트가 가능해졌다(코드 변경 아님, 환경
정합 조치).

## 신규 발굴 (2026-07-21 재점검, §38)

§1~37 및 남은 과제(외부 자원 필요 4건)와 겹치지 않는 후보를 재탐색(11차,
app/core 인프라·나머지 API 라우트로 범위 확대). `strategies.py::_get_owned`
IDOR 방지 로직은 확신도 낮음(라우트 레벨 통합테스트 필요, 비용 대비 가치
낮음)으로 보류. `kis.py`/`backtests.py`/`screener.py`/`alerts.py`는 대부분
얇은 CRUD/위임 래퍼로 뚜렷한 무테스트 고위험 로직을 찾지 못함.

## 38. `app/core/session.py` 서버측 세션 로직 테스트 커버리지 0 해소 ✅

모든 인증 요청이 거치는 보안 핵심 경로(Redis 기반 서버측 세션)인데
`create_session`/`get_session_user_id`/`destroy_session`을 직접 호출하는
테스트가 전무했다(§신규발굴 11차 1위 — 다른 라우트 테스트가 간접적으로만
거쳤을 뿐). `test_session.py` 신설(7건)로 슬라이딩 만료(조회 성공 시 TTL
갱신), 빈 sid 단락 처리(Redis 조회 자체를 시도하지 않음), 저장값이 정수로
파싱되지 않을 때(데이터 오염 등) 예외 대신 None 반환하는 방어, 존재하지
않는 sid 폐기의 무해성을 검증했다. `conftest.FakeRedis`를 확장한
`_TrackingRedis`로 `expire` 호출 인자를 기록해 슬라이딩 갱신 여부까지
정밀 확인했다. 실제 버그는 발견되지 않았으나(로직은 정확했음) 향후 회귀를
막는 안전망을 확보했다.

## 전체 코드 버그 검사 (2026-07-21, §39~§40)

사용자 요청으로 "문서 최신화 + 전체 코드 버그 검사"를 수행. 문서(CLAUDE.md·
docs/PRD.md·docs/CONVENTIONS.md·help/README.md·docs/improvements.md)는
review-fastapi/fork 점검 결과 대체로 최신 상태로 확인(PRD 의 `alerts` 테이블
누락은 문서 최상단에 이미 "역사적 기록" 면책 배너가 있어 수정 불필요로
판단). 백엔드 전체(review-fastapi)·프론트엔드 전체(review-nextjs)를 각각
전담 리뷰 에이전트로 재점검해 실제 버그 2건을 발견·수정했다.

## 39. `engine/main.py::_control_loop` 예외 미처리로 원격제어 마비 가능 — 수정 ✅

좁게 잡힌 `except (json.JSONDecodeError, KeyError, ValueError)` 밖의 예외
(예: Redis 순간 단절의 `ConnectionError`)가 `pubsub.get_message()`에서
발생하면 `_control_loop` 태스크 자체가 아무도 모르게 조용히 종료됐다.
`_heartbeat_loop`는 별도 태스크라 계속 살아 있어 `/api/engine/health`는
"정상"으로 보이므로, 이후 전략 start/stop 원격제어(웹의 "중지" 버튼 등)가
전부 무시돼도 운영자가 알아채기 어려웠다 — 진행 중이던 전략을 급히 멈춰야
하는 상황에서 중지 명령이 반영되지 않는 실질적 리스크. `_reconcile_loop`/
`_fill_notice_loop`와 동일한 패턴(넓은 `except Exception`으로 재구독 +
연속 실패 임계-교차 시 1회 critical 알림 + 성공 시 카운트 리셋)으로
수정했다. `test_control_loop.py` 신설(4건)로 정상 메시지 처리, 잘못된
메시지 스킵, 예외 발생 시 재구독하며 임계 도달 시 알림 발행, 복구 후 실패
카운트 리셋을 검증했다.

## 40. 프론트엔드 WS 중복 연결 + `formatRelativeTime` 도달불가 분기 — 수정 ✅

**WS 중복 연결(High)**: `RequireAuth`가 인증된 모든 화면에 항상
`<AlertCenter />`를 마운트하고 `AlertCenter`가 자체적으로 `useEventSocket`을
호출하는데, `app/monitor/page.tsx`의 `MonitorContent`도 별도로
`useEventSocket`을 호출하고 있었다. `useEventSocket`은 호출될 때마다 새
`WebSocket`을 만들었으므로, `/monitor` 접속 시 같은 클라이언트가 동일 이벤트
스트림을 소켓 2개로 중복 수신하고(포지션/주문 invalidate 등이 이벤트 1건당
2번 발생) 재접속 백오프도 독립적으로 동작해 서버 부하가 배가됐다.
`lib/useWebSocket.ts`를 탭당 소켓 1개만 유지하는 모듈 싱글턴으로 재작성해
호출부(AlertCenter/monitor 페이지) 변경 없이 근본 원인을 해소했다 —
구독자(핸들러)를 Set 으로 관리하고, 마지막 구독자가 해제될 때만 실제 소켓을
닫는다. `useWebSocket.test.tsx`에 다중 구독자 시나리오(소켓 1개만 생성·양쪽
모두 이벤트 수신·부분 언마운트 시 유지·전원 언마운트 시 닫힘) 테스트 추가.

**`formatRelativeTime` 도달불가 분기(Medium)**: `if (diffSec < 5) return
"방금 전"; if (diffSec < 0) return "곧";` 순서상 음수(diffSec<0, 미래
타임스탬프·시계 오차)는 항상 5보다 작아 먼저 걸리므로 `"곧"` 분기가 영원히
실행되지 않는 죽은 코드였다. 조건 순서를 바꿔 수정하고 `format.test.ts`에
미래 시각 케이스 테스트를 추가했다.

**환경 제약**: 이 환경은 호스트 `node_modules`에 vitest 가 설치돼 있지 않고
(frontend 패키지는 컨테이너 내부 설치가 원칙) Docker Desktop 도 오프라인이라
프론트엔드 변경분은 `npx tsc --noEmit`으로 타입 검사만 확인했고(내 변경
파일에 신규 에러 없음, 기존 vitest/playwright 관련 에러는 환경 전용이라
무관) 실제 `npm run test`(vitest) 실행은 하지 못했다 — Docker 복구 후
`docker compose exec frontend npm run test`로 재확인 필요.

## 남은 과제

| 순위 | 항목 | 이유 |
|------|------|------|
| 1 | fill_notice 실계정 검증 (§1) | 실전 전환의 마지막 관문 — 미검증 가정 3개 해소. 실계정 필요 |
| 2 | 0008 백필 감사 (§2) | id=23+24 병행 운용의 남은 전제(§5 해소로 긴급도는 낮아짐). 운영 DB 필요 |
| 3 | S3 오프사이트 백업 자격증명 발급·활성화 (§10) | 코드는 완료, 버킷·키 준비는 운영자 몫(외부 계정 필요) |
| 4 | 패닉셀 S9 신저가 비율 임계값 캘리브레이션 | §19 구현 시 잠정값(warn 0.10/panic 0.25)으로 둔 임계값 — 캐시가 충분히 쌓인 뒤 역사적 사례로 검증 필요 |

완료 이동: TTM A/B 재검증(§3, 2026-07-19) — 혼재로 옵트인 유지 종결. 체결 모델 정밀화
A/B(§4, 2026-07-19) — 영향도 미미(Δ≈0)로 기존 근사 유지 종결. 팩터 섹터 중립화
id=23 A/B(§20, 2026-07-19) — 반기 교차 혼재로 현행(미적용) 유지 종결. alerts
"더보기" 프론트 UI(§21 부수, 2026-07-19) — `AlertCenter.tsx` `useInfiniteQuery`
전환·이전 이력 로드 버튼 연동 완료.

## 41. 기관·외국인 수급(flow) 팩터 — 구현·PIT 검증 후 전략 등록 기각 ⚠️

**배경**: 가격·재무 기반 크로스섹션 팩터가 소진되어, id=23(균형 멀티팩터·저베타)과
상관이 낮은 새 return driver로 기관+외국인 투자자별 순매수(지속 accumulation) 수급
팩터를 후보로 추가했다(financial-expert 설계). 목표는 대체가 아니라 저상관 보완재.

**구현**(엔진 확장은 유지 — vol-harvest 게이트(§거절)와 동일 선례로, 전략은 기각해도
팩터 배선은 opt-in 능력으로 보존):
- `metrics/fetch.py::_fetch_net_purchases` — pykrx `get_market_net_purchases_of_equities`
  로 외국인+기관합계 기간 누적 순매수 '대금'을 시장×투자자에 걸쳐 종목별 합산(시장당
  1회 급 배치). 부분 실패는 중립 폴백, 전량 실패는 빈 프레임(리밸런싱 스킵 신호).
- `metrics/factors.py::compute_flow_norm` — [as_of−window, as_of] 누적 순매수 / 정규화
  분모(시총 또는 동일창 누적 거래대금). 단기 반전이 아닌 60~120일 지속 신호(회전율
  억제). `_compute_stock_scores`에 `score_flow` 카테고리 추가(z-score, 높을수록 순매수
  지속). `flow_norm` 컬럼 부재 시 전부 0(중립) → 기존 전략 무영향.
- `compute_universe_scores`: flow 가중치>0 일 때만 조회, 전량 실패 시 예외(모멘텀 팩터의
  전량실패 방어 패턴 재사용).
- 스키마: `FactorWeights.flow`(기본 0.0, 합=1.0 검증 포함)·`RebalanceSelection.flow_window`
  (기본 90)·`flow_denom`(mcap/value) 추가. 백테스트 provider(`backtests.py::_provider_with_flow`)
  ·실거래 러너(`rebalance_runner`)·`attribution._FACTOR_SCORE_COLS`에 배선.

**검증(PIT KOSPI200, 2021.1–2025.6, next_close+슬리피지, 왕복 실효비용≈0.33%)**:
id=23 팩터믹스를 0.8로 축소하고 flow=0.20 삽입, window∈{60,90,120}×denom∈{mcap,value}
6조합을 반기 2-fold 워크포워드. 기준 id=23: FULL ret **+130.0%** / Sharpe **1.04** /
alpha **+19.3%** / β0.57 / MDD −23.0% / 실회전율 106%.

| flow 변형 | FULL alpha | FULL Sharpe | id23 상관 | 50/50 결합 Sharpe |
|---|---|---|---|---|
| w60 mcap | +9.6% | 0.63 | +0.91 | 0.87 |
| w60 value | +9.9% | 0.69 | +0.90 | 0.90 |
| w90 mcap | +11.4% | 0.71 | +0.90 | 0.90 |
| w90 value | +8.8% | 0.62 | +0.90 | 0.87 |
| **w120 mcap** | **+14.5%** | **0.85** | +0.89 | 0.98 |
| w120 value | +7.5% | 0.56 | +0.87 | 0.85 |

**기각 사유(3중, 모두 정량)**:
1. **워크포워드 불일치**: 어떤 변형도 양 반기 모두에서 id=23을 alpha·Sharpe로 이기지
   못했다. 최선(w120 mcap)도 H1은 근소 우위(+3.2% vs +1.4%)지만 H2에서 명확히 열위
   (+26.3% vs +36.3%, Sharpe 1.44 vs 1.73) — "일부우위"에 그침.
2. **직교성 없음(보완재 명분 붕괴)**: id=23 일간수익과 상관 **+0.87~+0.91**로 높다.
   50/50 결합 Sharpe(0.85~0.98)가 **모든 변형에서 id=23 단독(1.04) 미만** — 섞으면
   위험조정수익이 오히려 나빠진다(분산 기여 음(−)).
3. **알파 희석**: 단독 alpha(+7.5%~+14.5%)가 id=23(+19.3%)보다 낮아, flow 편입은 코어
   알파를 희석할 뿐이다.

**해석**: KOSPI200 대형주에서 외국인+기관 누적 순매수는 강세장에서 기관이 담는
퀄리티·모멘텀 대형주 바스켓과 사실상 겹친다 — id=23이 이미 노출한 팩터의 중복 프록시라
'새 return driver'가 아니었다. denom은 mcap이 value보다 일관 우월, window는 길수록(120)
개선(지속 가설 방향과 일치)했으나 어느 조합도 채택 문턱을 못 넘음. 회전율은 지속(장기
누적) 설계 덕에 폭증하지 않았다(94~125%, 코어 106%와 유사) — 회전율은 문제 아니었음.

**결론**: 신규 전략 미등록. 팩터 배선(엔진 확장)은 flow=0 기본으로 기존 전략에 무영향인
opt-in 능력으로 보존한다. 검증 스크립트: `backend/scripts/validate_flow_factor.py`.

## 42. 잔차(베타·사이즈 조정) 모멘텀 팩터 — 구현·PIT 검증 후 전략 등록 기각 ⚠️

**배경**: §41(flow) 기각 후 financial-expert 2순위(저비용 스카우트) 제안. 팩터 IC/IR
분석에서 원시 모멘텀은 id=23(저베타 방어형)에서 IR −0.18로 역효과였다 — 가설은 "저베타
전략에선 원시 모멘텀이 사실상 베타/변동성 베팅으로 변질됐다"였다. 개별 종목 월수익률을
시장(KOSPI200)에 회귀해 시장·베타 성분을 걷어낸 **잔차의 형성창 누적**(Blitz·Huij·Martens
2011, "Residual Momentum")으로 원시 모멘텀을 대체하면 그 오염이 해소되는지 검증. 데이터는
종가+KOSPI200 지수만 필요(벤치마크·레짐이 이미 쓰는 소스 재사용, 외부 데이터 불요).

**구현**(엔진 확장 유지 — §41·vol-harvest 선례대로 전략은 기각해도 팩터 배선은 opt-in 보존):
- `metrics/factors.py::compute_residual_momentum_panel` — 종가 패널·시장 종가로 월수익률을
  만들어 각 종목을 시장에 롤링 회귀(reg_window 개월), 잔차의 형성창(최근 mom_window 개월,
  최근 skip 개월 제외) 평균/표준편차(잔차 정보비율)를 resid_mom 으로 산출. std 로 나눠
  고변동 종목 과대노출 억제(Blitz 핵심). 미래참조 없음(월말 리샘플·회귀·형성 모두 as_of
  컷). 사이즈 조정은 시계열 회귀 대신 스코어러 크로스섹션 사이즈 중립화로 처리(SMB 시계열
  불요). `compute_residual_momentum` — 실거래용 종가+지수 조회 래퍼.
- `_compute_stock_scores`에 `score_residual_momentum` 카테고리 추가(원시 `score_momentum`과
  **별개 슬롯** — 원시 필드 보존, 옵트인 병행 대체). 컬럼 부재 시 0(중립) → 기존 전략 무영향.
- 스키마: `FactorWeights.residual_momentum`(기본 0.0, 합=1.0 검증)·`RebalanceSelection`
  .resid_mom_reg_window/window/skip 추가. 백테스트 provider(`_provider_with_resid_mom`,
  적재된 종가 패널+벤치마크 재사용·외부조회 없음)·실거래 러너·`attribution._FACTOR_SCORE_COLS`
  ·`compute_universe_scores`(가중치>0 시만 조회, 전량 실패 예외)에 배선.

**검증(PIT KOSPI200 280종목 합집합, 2021.1–2025.6, next_close+슬리피지, 왕복≈0.33%)**:
id=23 momentum 가중치(0.2)를 residual_momentum 으로 그대로 스왑, (reg,win,skip)
∈{36,24}×{11,6}×{1} 4조합을 반기 2-fold 워크포워드. 기준 id=23: FULL ret **+130.0%** /
Sharpe **1.04** / alpha **+19.3%** / β0.57 / MDD −23.0% / 실회전율 106%.

| 잔차 변형 | FULL alpha | FULL Sharpe | FULL MDD | 실회전율 | id23 상관 | 결합 Sharpe |
|---|---|---|---|---|---|---|
| r36 w11 s1 | +11.8% | 0.80 | −20.6% | 95% | +0.91 | 0.95 |
| **r24 w11 s1** | **+13.4%** | **0.86** | −20.1% | 103% | +0.91 | 0.98 |
| r36 w6 s1 | +11.2% | 0.76 | −26.7% | 99% | +0.92 | 0.93 |
| r24 w6 s1 | +10.3% | 0.69 | −27.4% | 97% | +0.90 | 0.90 |

**원시 vs 잔차 모멘텀 단독 IC/IR(FULL, 동일 PIT 구간)** — 핵심 진단:
| 팩터 | IC | IR | 적중률 | 롱숏수익 |
|---|---|---|---|---|
| 원시 모멘텀 | −0.033 | −0.27 | 0.27 | −0.251 |
| 잔차 r24 w11 | −0.056 | −1.41 | 0.40 | −0.424 |
| 잔차 r36 w11 | −0.081 | −1.38 | 0.10 | −0.418 |

**기각 사유(3중, 모두 정량)**:
1. **가설 반증 — 역효과 악화**: 잔차 모멘텀 IC/IR(−0.02~−0.08 / −0.30~−1.41)이 원시
   모멘텀(−0.033/−0.27)보다 **더 음(−)**. 시장·베타 성분을 회귀로 걷어내도 반전이 해소되긴커녕
   **증폭**됐다. 즉 이 유니버스(2021–2025 KOSPI200 대형주)에서 모멘텀의 음(−) 예측력은
   베타 기인이 아니라 **고유(idiosyncratic) 반전**이며, 잔차화가 바로 그 고유성만 남겨 악화시킨다.
2. **워크포워드 전면 열위**: 4변형 모두 양 반기에서 id=23을 alpha·Sharpe로 이기지 못함(전부
   "열위"). 최선(r24 w11) FULL Sharpe 0.86·alpha +13.4%로 id=23(1.04·+19.3%) 명확 하회.
3. **직교성 없음**: id=23 일간수익과 상관 **+0.90~+0.92**로 §41(flow)과 동일하게 높다.
   50/50 결합 Sharpe(0.90~0.98)가 모든 변형에서 id=23 단독(1.04) 미만 — 분산 기여 음(−).

**해석**: 잔차 변형은 β를 0.52~0.53으로 낮추고 일부는 MDD도 소폭 개선(r24 w11 −20.1%)했으나
알파·총수익 손실이 이를 압도한다. 원시 모멘텀이 역효과인 근본 원인은 "저베타 전략의 베타
베팅 변질"이 아니라 이 방어형 대형주 유니버스에서 모멘텀 자체가 반전 신호이기 때문이며,
잔차화는 그 반전을 제거하는 게 아니라 순화(純化)한다. financial-expert가 경고한 "알파
marginal 가능성"보다 나쁜 결과(팩터 IC 자체가 음)다.

**결론**: 신규 전략 미등록. 팩터 배선은 residual_momentum=0 기본으로 기존 전략에 무영향인
opt-in 능력으로 보존한다. 검증 스크립트: `backend/scripts/validate_residual_momentum.py`.

## 43. PEAD(실적 서프라이즈 드리프트) 팩터 — 구현·PIT 검증 후 전략 등록 기각 ⚠️

**배경**: §41(flow)·§42(resid-mom) 연속 기각 후 financial-expert 3순위 제안. PEAD
(Post-Earnings-Announcement Drift)는 실적 서프라이즈 방향으로 발표 후 수주간 수익률이
지속(drift)한다는 아노말리다. 컨센서스 추정치가 없어 기대치는 **계절적 랜덤워크**(전년
동기)로 프록시하고, 표준화 기대외 이익(SUE)을 새 팩터 `score_pead` 로 삼는다. 앞선 두
시도가 id=23과 상관 +0.87~0.92로 직교성이 없어 기각됐음을 감안하고, 대형주 KOSPI200이
id=23의 저변동·퀄리티 알파에 수렴하는 구조일 가능성을 열어두고 진행.

**구현**(엔진 확장 유지 — §41·§42·vol-harvest 선례대로 전략은 기각해도 팩터 배선은 opt-in
보존):
- `opendart.py`: **접수일(rcept_dt) 기준 엄격한 PIT 정렬**이 핵심. `disclosure_calendar`
  가 정기공시 목록(list.json, pblntf_ty="A")에서 각 보고서의 실제 접수일을 파싱·중복제거
  (원공시+정정공시 시 최초 접수일 유지). `_single_quarter_net_income` 는 분기 누적치를
  해제해 단일분기 순이익 산출. `pead_sue_by_symbol` 은 as_of 시점에 **접수일이 도래한
  (rcept_dt≤as_of) 보고서만** 취해 단일분기 순이익 YoY 서프라이즈 시계열을 만들고 최근
  lookback_q 분기 표준편차로 표준화(SUE). 전년 동기·직전분기는 모두 과거라 항상 이미
  공시됨 → 룩어헤드 없음.
- `factors.py::compute_pead_sue` 래퍼 + `_compute_stock_scores` 에 `score_pead` 카테고리
  추가(z-score, 높을수록 양(+) 서프라이즈). 컬럼 부재 시 0(중립) → 기존 전략 무영향.
- 스키마: `FactorWeights.pead`(기본 0.0, 합=1.0 검증)·`RebalanceSelection.pead_lookback_q`
  (기본 8) 추가. 백테스트 provider(`_provider_with_pead`)·실거래 러너(`rebalance_runner`)
  ·`attribution._FACTOR_SCORE_COLS`·`compute_universe_scores`(가중치>0 시만 조회, 전량
  실패 예외)에 배선.
- **부수 버그 2건 수정**: (1) `portfolio.py` 백테스트 팩터 컬럼 화이트리스트에 `pead_sue`
  누락 → 스코어러 직전 조용히 드롭돼 팩터가 무효화되던 버그(초기 검증에서 pead 변형이
  id=23과 완전 동일하게 나온 원인). (2) `opendart.annual_metrics`/`_period_metrics`/
  `disclosure_calendar` 가 **조회 실패(SSL 타임아웃·요율초과)의 all-None 결과까지 캐시**해
  일시 실패가 프로세스 수명 동안 굳어져 재시도를 막던 잠복 버그(모듈 docstring 의 "실패는
  캐시하지 않는다" 규약이 `_ACCOUNTS_CACHE` 에만 적용되고 파생결과 캐시엔 누락돼 있었음).
  성공(accounts truthy·data not None)일 때만 캐시하도록 수정 — PEAD 는 분기 원자료 다수를
  연쇄 조회해 이 재시도 보장이 특히 중요.

**PIT 정렬 검증(단위테스트, `tests/test_pead_factor.py` 17개 통과)**: 접수일 기준 미래참조
차단을 명시적으로 못박음 — as_of 이후 접수된(rcept_dt>as_of) 분기의 순이익을 ±1e9로
뒤집어도 SUE 불변(미래참조 없음), 접수일 당일(==as_of)은 포함·다음날은 배제(게이트가
접수일에 정확히 걸림), 단일분기 누적해제 산술, report_nm 파싱(정정공시 접두사·비정기공시
거부), 정정공시 중복 시 최초 접수일 유지.

**검증(PIT KOSPI200 269종목 합집합, 2021.1–2025.6, next_close+슬리피지, 왕복≈0.33%≥0.23%)**:
id=23 팩터믹스를 0.8로 축소하고 pead=0.20 삽입, lookback_q∈{6,8,12}를 반기 2-fold
워크포워드. 기준 id=23: FULL ret **+130.0%** / Sharpe **1.04** / alpha **+19.3%** / β0.57 /
MDD −23.0% / 실회전율 106.4%.

| pead 변형 | FULL alpha | FULL Sharpe | FULL MDD | 실회전율 | id23 상관 | 결합 Sharpe |
|---|---|---|---|---|---|---|
| lb6  | +12.8% | 0.76 | −21.5% | 107.4% | +0.96 | 0.91 |
| lb8  | +15.6% | 0.89 | −22.2% | 110.3% | +0.96 | 0.98 |
| lb12 | +16.4% | 0.92 | −21.5% | 112.7% | +0.97 | 0.99 |

**PEAD 단독 IC/IR(FULL, 분기, 동일 PIT 구간)** — 핵심 진단:
| 팩터 | IC | IR | 적중률 | 롱숏수익 | n |
|---|---|---|---|---|---|
| pead lb6  | −0.015 | −0.24 | 0.27 | −0.185 | 11 |
| pead lb8  | −0.013 | −0.20 | 0.36 | −0.103 | 11 |
| pead lb12 | −0.013 | −0.19 | 0.45 | −0.148 | 11 |

**기각 사유(3중, 모두 정량)**:
1. **예측력 없음(음(−) IC) — 가설 반증**: SUE 프록시 단독 IC가 −0.013~−0.015, IR −0.19~
   −0.24로 예측력이 없고 오히려 약한 음(−)이다. financial-expert가 경고한 "컨센서스 부재로
   SUE 프록시가 약할 수 있다"보다 나쁜 결과 — 계절적 랜덤워크 기대치로는 이 대형주
   유니버스에서 PEAD 드리프트를 잡아내지 못한다.
2. **직교성 전무(보완재 명분 붕괴)**: id=23 일간수익과 상관 **+0.96~+0.97**로 §41(flow
   +0.87~0.91)·§42(resid +0.90~0.92)보다도 **더 높다**. 50/50 결합 Sharpe(0.91~0.99)가
   모든 변형에서 id=23 단독(1.04) 미만 — 분산 기여 음(−).
3. **워크포워드·알파 열위**: 어떤 변형도 양 반기 모두에서 id=23을 이기지 못함(lb6/lb8은
   H1만 근소 우위·H2 명확 열위="일부우위", lb12는 양 반기 열위). 단독 alpha(+12.8~16.4%)가
   id=23(+19.3%)보다 낮아 코어 알파를 희석할 뿐이다.

**캐던스 A/B(분기 vs 월간, pead lb12, FULL)**: PEAD 드리프트(~60거래일 창)와 분기 리밸런싱의
불일치가 알파를 깎는지 확인. quarterly: id23 a+19.3%/shp1.04 vs pead a+16.4%/shp0.92.
monthly: id23 a+11.7%/shp0.67/turn67.6%(월간이 분기보다 열위 — 기존 결론 재확인). **월간
pead lb12 줄은 세션 중단으로 미확보**했으나, 분기 결과만으로도 기각 결론은 확정적이라
재실행하지 않았다(월간은 id=23 자체가 이미 분기 대비 크게 열위라 캐던스가 문제의 본질이
아님 — 문제는 SUE 프록시의 예측력 부재).

**해석**: KOSPI200 대형주에서 계절적 랜덤워크 SUE는 (a) 컨센서스 없는 기대치라 진짜
서프라이즈를 못 잡고, (b) 잡아낸 신호마저 id=23이 이미 노출한 성장(growth, YoY)·퀄리티
팩터와 사실상 중복(상관 +0.96~0.97)이다 — 별개 return driver가 아니었다. lookback_q는
길수록(12) 소폭 개선됐으나 어느 조합도 채택 문턱을 못 넘음. 회전율은 정기공시 주기
신호라 폭증하지 않았다(107~113%, 코어 106%와 유사).

**결론**: 신규 전략 미등록. 팩터 배선(엔진 확장·PIT 접수일 정렬 인프라)은 pead=0 기본으로
기존 전략에 무영향인 opt-in 능력으로 보존한다(부수 수정한 opendart 실패-캐시 버그·portfolio
화이트리스트 버그는 존치 — 다른 OpenDART 팩터에도 이로운 일반 개선). 검증 스크립트:
`backend/scripts/validate_pead_factor.py`.

## 신규 발굴 (2026-07-31, §44~§47)

발굴 근거: 2026-06~07 KRX 폭락(7월 월간 −22.19%, 6월 고점 대비 장중 −43.9%, 7-31 하루
+17.91% 반등) 구간을 financial-expert가 웹 조사로 정리한 뒤, "이런 사건을 지표로 미리
확인하고 대처할 수 있는가"를 코드베이스에 대조해 나온 공백들. 시장 사실관계와 id=23
함의는 조사 리포트를, 데이터 소스 규격은 `app/services/data/kofia.py` docstring을 참조.

## 44. 엔진에 거래정지·시장 서킷브레이커 개념 부재 — 해소 ✅

2026년 7월 시장 CB가 9회 발동해 매번 20~30분 체결이 불가했고 재개 직후 호가는 붕괴
상태였는데, 엔진에는 정지 상태 개념 자체가 없어 러너가 그대로 주문을 냈다.

**원인은 신규 연동 부재가 아니라 데이터 유실**이었다 — KIS `inquire-price` 응답에
`temp_stop_yn`·`iscd_stat_cls_code`가 실려 오는데 `get_quote()`의 `Quote` 정규화에서
가격 필드만 남기고 탈락시키고 있었다.

**구현(PR #109)**: `Quote.halted`·`status_code` 추가 + `is_halted_status()`를 브로커
계층 단일 출처로. `engine/halt.py` 신설 — 동시 정지 비율로 시장 CB를 간접 판정하는 순수
상태기계(`NORMAL→HALTED→COOLDOWN→NORMAL`). 게이트는 모든 주문이 지나는
`base_runner._place`에 `live_gate`와 같은 자리로 배선.

설계 결정 3가지:
- **재개 직후 유예(COOLDOWN)**: 정지가 풀려도 곧바로 재개하지 않는다. 붕괴된 호가에
  시장가가 꽂히는 것이 정지 자체보다 위험하다.
- **표본 부족 시 판정 보류**: `min_sample` 미만이면 판정하지 않는다. 2~3종목 전략에서
  개별 VI를 시장 CB로 오판하면 정상장에서 매매가 통째로 멈춘다. 러너별로 두면 소수 종목
  전략이 표본을 못 채우므로 프로세스 전역 모니터로 관측을 합친다.
- **러너를 멈추지 않고 주문만 막는다**: 러너를 멈추면 관측도 멈춰 시장 재개를 실제
  관측이 아니라 타이머로만 판단하게 된다. 시세 조회 실패는 '정지 모름'이므로 통과시킨다
  (조회 장애가 매매 전면 중단이 되지 않게).

**남은 검증**: `temp_stop_yn='Y'`·상태코드 58 판정은 KIS 문서 기준값이며 **실계좌 응답으로
교차 확인하지 않았다**. 특히 시장 CB 중 개별 종목에 `temp_stop_yn=Y`가 오는지가 간접
판정의 전제인데 미확인 — 이 가정이 틀리면 시장 CB 감지는 작동하지 않는다(종목 정지
게이트는 그대로 유효). 모의투자 계좌로 VI 발동 종목을 조회해 확인할 것.

## 44-1. KRX 로그인 재시도 폭주로 인증 차단 — 해소 ✅

§47 검증 중 발견. `_build_pit_pool` 은 조회 구간의 **월마다** `index_members` 를 부르고,
세션이 유효하지 않으면 그때마다 로그인을 새로 시도한다. 19개월 구간 백테스트 한 번에
로그인이 19회 나갔고, 실제로 KRX 가 로그인 응답을 JSON 이 아닌 차단 페이지로 돌려주는
상태가 됐다.

**가장 위험한 부분은 차단 그 자체가 아니라 실패가 조용했다는 점이다.** 차단되면 모든
PIT 조회가 0종목을 반환하고, 백테스트는 **빈 패널 위에서 '성공'하며 무의미한 수치를
낸다**(실제로 그 쓰레기값을 결과로 받았다). 검증 스크립트가 그 수치를 그대로 보고했다면
잘못된 결론이 로드맵에 박혔을 것이다.

**수정**: (a) 로그인 실패 시 300초 쿨다운을 둬 재시도 폭주를 막는다(예외뿐 아니라 '예외
없이 None 반환' 경로도 실패로 처리). (b) PIT 후보풀이 전 구간 0종목이면 에러 로그로
드러낸다. (c) 검증 스크립트는 패널이 비면 즉시 중단한다.

**남은 한계**: 차단 해제까지 기다려야 하며, 해제 시점은 알 수 없다. 장기 구간 PIT
백테스트는 월별 조회를 캐시하거나 세션 수명을 늘리는 개선이 더 필요하다.

**후속(§48 에서 해소)**: 위 수정은 이 사고 하나를 막았을 뿐, **실패가 조용할 수 있는
구조 자체**는 세 데이터 모듈에 그대로 남아 있었다. 그 구조를 없앤 작업이 §48 이다.

## 45. 사전(취약성) 지표 계층 부재 — 데이터 수집만 완료 ⚠️

`metrics/panic.py`의 S1~S9는 전부 가격·거래대금·브레드스 기반이라 **설계상 동시지표**다.
모듈 docstring이 스스로 "자본항복은 통계적으로 바닥 근처에 발생 → 매매 신호 아님"을
명시한다. 즉 바닥 판정용이지 사전 경보가 아니다. 2026-07 구간에서도 7-13이나 7-28에야
켜졌을 텐데 그때는 이미 −30%다.

**관측 대상이 다르다**: 폭락의 동력이 신용융자 38조와 레버리지 ETF의 강제 청산 나선이었다면
봐야 할 것은 가격(결과)이 아니라 레버리지가 쌓이는 과정(원인)이고, 그것은 **가격이 오르는
동안** 몇 달에 걸쳐 관측된다.

**완료(PR #110)**: `app/services/data/kofia.py` — 금투협 FreeSIS 증시자금(미수금·반대매매
금액·비중) 일별 시계열. KRX MDC STAT 계열과 달리 인증 없이 열리고 **2008년까지 소급**된다
(§46 오탐률 측정의 전제). 응답에 컬럼 라벨이 없어 산술관계로 식별했다(비중 =
반대매매금액[t]/미수금[t−1]×100, 41/41 성립·최대 오차 0.05%p). 의미 미확정 컬럼은 이름을
붙이지 않고 `raw`로만 통과시킨다.

**1순위 지표 확보 완료**: 신용융자 잔고는 FreeSIS의 **별도 통계표**(`...070BO`)에 있었고
(합계 `TMPV2`가 2026-06-01 37.68조 → 07-30 32.15조 −14.7%로 보도치와 일치, `TMPV2 =
TMPV3 + TMPV4` 42/42 성립), 레버리지 ETF는 KRX 인증 세션으로 확보했다(2026-07-31 기준
62종목 22.72조, 그중 **단일종목 14종목 8.62조**). 부수적으로 KRX ETF 엔드포인트가 휴장일에
빈 응답이 아니라 직전 영업일 데이터를 그대로 준다는 사실을 발견해, 조회 **전에** 영업일로
스냅하도록 고쳤다(그러지 않으면 반환 날짜가 거짓 라벨이 된다).

**게이지 본체 — 구현했으나 §46 검증에서 기각.** 신용융자 롤링 z(수준)·60거래일 증가율
(속도)·반대매매 20일 평균(압력)을 결합해 노출을 연속 스케일하는 설계였다. 이진 on/off가
아닌 이유는 기존 레짐 오버레이의 이진 스위치가 2026-07에 최악 특성(늦게 끄고 7-31 반등을
놓침)을 보였기 때문이다. 결과는 §46 참고 — **배선하지 않는다.**

**실측 경고 — 반대매매 비중 단독 사용 금지**: 이 지표는 평온/스트레스는 구분하지만
**낙폭 규모를 구분하지 못한다.**

| 국면 | 평균 | 최대 |
|---|---|---|
| 2008 금융위기 | 9.7 | 23.0 |
| **2022-06 긴축** | **8.7** | **13.1** |
| 2026-07 폭락 | — | 10.5 |
| 2020 코로나 | 6.1 | 8.5 |
| 2026-01 평온 | 1.0 | 1.7 |

2022-06 일반 약세장이 2026-07 −44% 폭락보다 높다. 또 2026-06-09에 10.5를 찍은 뒤에도
지수는 6-22 사상 최고까지 올랐다 — 오탐이 실물로 확인된다.

## 46. 취약성 게이지 오탐률 사전 측정 — 측정 완료, **게이지 기각** ⚠️

**결론: 채택 문턱 미달. `exposure_scale` 을 노출 제어에 배선하지 않는다.** 모듈
(`app/services/metrics/vulnerability.py`)과 검증 스크립트
(`scripts/validate_vulnerability_gauge.py`)는 관측·재현용으로만 존치하며, 모듈
docstring 상단에 기각 결론을 명시했다.

**절차**: 임계값(z 1.0/2.0, 증가율 10%/20%, 반대매매 5%/8%)을 오탐률 측정 **전에**
일반 원칙만으로 확정해 코드와 테스트에 고정한 뒤, 2006~2026 전 구간 5,094 거래일에
롤링으로 돌렸다. 경보 = 점수 50 이상, 적중 = 이후 60거래일 내 KOSPI −15% 이상 하락,
연속 경보는 에피소드로 묶음(같은 국면을 여러 번 세면 오탐률이 왜곡된다).

**결과 — 3중 실패**

1. **오탐 73%** (에피소드 15건 중 11건 헛울림). 경보일이 평가일의 19.2%로 지나치게 잦다.
2. **주요 폭락 4건을 전부 놓쳤다.** 경보 직전 점수: 2008-10 금융위기 **25.0** /
   2011-08 유럽위기 **0.0** / 2020-03 코로나 **10.8** / 2022-06 긴축 **20.0** — 모두
   임계 50 미만. 유일하게 사전 포착한 것이 **설계의 계기였던 2026-07(54.6)** 하나다.
   전형적인 단일 사건 과적합 신호이며, 임계를 원칙으로 정했더라도 **지표 구성 자체가
   그 사건의 서사에서 나왔다**는 사실은 남는다.
3. **기회비용 80.8%p.** 경보 구간 상승장에서 노출 축소로 놓친 수익 누계. 최악은
   2025-06~2026-06(207일): 경보를 켠 채 KOSPI 가 **+187.3%** 올랐다. 판정상 '적중'
   (이후 −38.6%)이지만 방어 이득보다 기회비용이 압도적이다. 2020-06~2021-05(150일)은
   경보 중 +45.4%, 이후 최저 −7.8% 로 순수 손실이었다.

**임계 재조정으로 살리지 않았다.** 결과를 보고 임계를 맞추는 것이 바로 이 절차가
막으려던 사후 확증편향이다. 되살리려면 지표 구성을 (2026-07 과 무관한 근거로) 다시
설계하고 오탐률을 처음부터 다시 재야 한다.

**남는 자산**: 수집 계층(§45의 `kofia.py`·`krx_index.etf_leverage_exposure`)은 기각과
무관하게 유효하다 — 신용융자·반대매매·레버리지 ETF 잔고는 대시보드 관측값으로 쓸 수
있고, 다른 설계의 입력으로도 재사용된다.

**교훈(재발명 방지)**: "폭락 직후 그 사건을 설명하는 지표를 만들면 그 사건만 맞힌다."
사전 지표 후보는 **사건 이전에 독립적으로 존재하던 근거**에서 출발해야 하며, 채택
판정에는 적중률뿐 아니라 **기회비용**을 반드시 포함해야 한다(적중해도 손해일 수 있다 —
207일 에피소드가 그 예다).

## 47. id=23의 2026-07 구간 4-arm 검증 — **스크립트 완성, 실행 보류** ⏸

> **전제 정정(2026-08-01)**: 이 항목은 "P2 패닉 오버레이 자연실험"으로 세워졌으나,
> **id=23 config 에는 `panic_overlay` 가 설정돼 있지 않다**(실제 확인:
> `cadence=quarterly, regime=True, panic=False`). 따라서 '패닉 off' arm 은 현행과
> 동일해지고, 이 구간으로 P2 오버레이를 검증할 수는 없다. 4-arm 설계는 **레짐 필터**
> 검증으로서는 그대로 유효하다. P2 검증이 필요하면 `panic_overlay` 를 켠 별도 변형을
> arm 으로 추가해야 한다.

> **미완 사유**: KRX 로그인이 차단돼 PIT KOSPI200 조회가 0종목을 반환하는 상태다.
> 원인은 §44-1(아래) — 차단이 풀린 뒤 `backend/scripts/validate_id23_crash_2026.py`
> 재실행 필요. **이 구간에서 얻은 수치는 전부 빈 패널 위의 값이므로 폐기했다.**

> **2차 시도(2026-08-05) — 실행했으나 결과 폐기.** PIT 차단은 풀려 있었다(KOSPI200
> 2026-06-01·07-01·07-30 모두 200종목, union 221종목으로 패널 384×221 정상 구성).
> 그런데 **다른 경로가 막혀 있었다** — `metrics/fetch.py` 의 pykrx 조회(펀더멘털·지수
> OHLCV)가 전부 실패했다. 실패 형태는 §44-1 과 동일한 차단 시그니처(`login_krx` 의
> `resp.json()` 이 `JSONDecodeError: Expecting value: line 13 column 1` — JSON 대신 HTML).
> 즉 **KRX MDC 경로(`krx_index`)는 살아 있고 pykrx 로그인 경로만 막힌** 상태다.
>
> 결과가 무의미하다는 증거는 수치 자체에 있다:
> - 3개 arm(현행 / 패닉off / 레짐+패닉off)의 지표가 **바이트 단위로 동일**하다
>   (ret −24.2%, beta 0.31, shp −2.91). 레짐 시계열이 비어 오버레이가 **한 번도
>   발동하지 않았기** 때문이다("이벤트 없음"). arm 간 대조가 아예 성립하지 않았다.
> - 리밸런싱일(2026-05-04·07-01) 양 시장 펀더멘털이 **전량 조회 실패** → 밸류 팩터가
>   통째로 중립 처리된 채 선정이 돌았다.
> - 스크립트의 "반증 조건 충족(50% 이상)" 출력은 실제 발견이 아니라 gap=0.00 에 의한
>   **0/0 인공물**(share=inf)이다. 이 줄을 결론으로 읽으면 안 된다.
>
> **드러난 구조적 문제**: §48 은 `krx_index`·`opendart`·`kofia` 세 모듈을 전환했지만
> **`app/services/metrics/fetch.py`(pykrx 경로)는 범위 밖이었다.** 이 모듈은
> `_fetch_per_market` 에서 `except Exception` → 빈 DataFrame 을 반환해, 밸류 팩터와
> 레짐 필터의 입력이 조용히 비어도 백테스트가 '성공'한다 — §44-1 과 같은 형태가 같은
> 저장소에 남아 있었고, 이번 실행이 그 결과를 실물로 보여줬다. **§47 재실행보다 이
> 구멍을 먼저 막아야 한다** — 안 그러면 차단이 풀린 날 또 빈 입력 위의 수치를 얻는다.

VKOSPI 96.94(2009년 집계 이후 최고)·이틀 연속 서킷브레이커(사상 최초)·7-13 −8.95% 구간은
`metrics/panic.py`의 S1·S2·S5·S8이 거의 확실히 panic 라벨을 찍는 국면이라, 오버레이
동작을 확인하기에 좋은 자연 실험 구간이다.

**설계**: PIT KOSPI200 only, 4-arm(현행 / 패닉 off / 레짐+패닉 off / B&H), 종료일
2026-07-30·07-31 **양쪽 병기**, up-beta/down-beta 분리, `regime_exit`·`panic_confirm`
이벤트 로그의 **정확한 발생 일자 추출**이 가설 판별의 직접 증거.

**판정 주의**:
- 표본 약 22거래일·일간 변동성 6%대라 alpha 추정의 표준오차가 추정치보다 크다.
  **이 구간 단독으로는 통계적 판정을 시도하지 말고** 기술통계로만 보고할 것.
- 지수 −22% 구간에서 β0.6 포트폴리오의 excess는 크게 (+)로 나오는데 이는 알파가 아니라
  **베타 부족의 산술적 부산물**이다. `id23-lowbeta-excess-artifact`의 거울상이며 위험
  방향만 반대다. 판정은 alpha/Sharpe로.
- **반증 조건**: (현행 − 레짐off) 차이의 50% 이상이 7-31 하루에서 발생하면 단일일 의존이므로
  **어느 결론도 채택 불가**로 보고한다.

**비용·체결 민감도(이 구간에서는 결과를 지배)**: 슬리피지 5→25→50bps 스윕(VKOSPI 80~97
구간에서 5bps 편도는 심각한 과소평가), CB 발동 5일(6-23·7-7·7-13·7-28·7-29) 거래금지
시나리오, 체결가정 익일종가 vs 시가 vs VWAP 교차확인(7-30 종가 신호 → 7-31 종가 체결은
+17.91%를 무상 취득하는 구조), 상·하한가 제약 재확인(7-31 실제 상한가 3종목 발생 —
`price-limit-ab-negligible` 결론의 예외 구간일 수 있음).

**전제**: 2026년 6·7월 OHLCV와 정기변경 반영 PIT KOSPI200 구성의 DB 적재 여부 확인이 선행.
미적재면 적재부터 해야 하며 그 전엔 어떤 검증도 불가능하다.

**해소 경로(2026-08-06)**: 원인이던 `metrics/fetch.py` 의 조용한 실패를 걷어내고
확정 과거 데이터를 로컬에 영구 저장하는 작업으로 닫는다. 설계는
`docs/superpowers/specs/2026-08-06-local-persistent-store-design.md`, 계획은
`docs/superpowers/plans/2026-08-06-local-persistent-store.md`. §47 재검증은 이
작업이 끝나고 pykrx 차단이 풀린 뒤에 다시 돌린다.

> **3차 시도(2026-08-16) — 실행됐으나 결론 보류.** pykrx 차단이 풀려 스크립트가
> 처음으로 끝까지 완주했다(이전처럼 조용히 빈 프레임을 삼키지 않고, 실패는
> `SourceUnavailableError` 로 제대로 드러났다). 그런데 결과 수치가 **2차 시도(폐기)와
> 소수점까지 완전히 동일**하다(ret −24.2%, beta 0.31, shp −2.91, 3개 arm 전부 이벤트
> 없음). 원인 후보 둘을 아직 못 갈랐다: (a) 과거 확정 데이터라 재현 가능한 참값이다,
> (b) 레짐/패닉 파이프라인에 §48 이 못 잡은 다른 형태의 데이터 결손이 여전히 남아있다
> — 워밍업 구간(2025-02~03) 내내 `compute_panic_series` 의 일별 등락률 조회가
> `get_nearest_business_day_in_a_week` 의 `IndexError`(pykrx 자체 버그)로 반복
> 실패하는 게 로그에 보인다. KOSPI 종가/MA200 비율을 직접 대조해 가르려 했으나,
> 검증 중 KRX 재로그인을 짧은 간격으로 여러 번 날려(앱 세션 관리를 우회한 원시 호출)
> 로그인이 다시 막혔다 — **§44-1 과 같은 시그니처지만 이번엔 자초한 쿨다운으로 보인다**
> (같은 세션 안에서 앱 경유 로그인은 두 번 다 성공했었다). 별도로, 이 검증의 원래
> 목적(P2 패닉 오버레이 확인)은 **id=23 의 현재 운영 config 에 `panic_overlay` 키
> 자체가 없어** 애초에 테스트 불가능하다는 것도 이번에 확인했다 — 데이터 결손 여부와
> 무관하게 별도로 처리해야 하는 실제 배선 공백이다. 쿨다운이 풀리면 KOSPI 종가/MA200
> 비율 직접 대조부터 재개할 것.

## 48. 외부 데이터 소스의 조용한 실패 — 해소 ✅

§44-1 의 근본 구조. `krx_index`·`opendart`·`kofia` 는 외부 호출이 실패해도 예외 대신 빈
값을 반환했다(세 모듈 합쳐 49곳). 문제는 실패가 감춰진 것 자체가 아니라 **실패한 빈 값과
정상적으로 빈 값이 같은 값**이라 호출자가 구분할 수 없었다는 점이다. `opendart._get` 은
미설정·네트워크 실패·에러 status·무자료의 **네 가지가 전부 `None`** 이었고, 특히 일일
20,000건 한도 초과(`020`)가 "조회된 데이터 없음"(`013`)과 구분되지 않아 **한도를 소진하면
전 종목이 조용히 '재무 정보 없음'** 이 됐다. status 코드는 응답에 들어 있었는데 읽고도
로그로만 흘리고 있었다.

**경계를 셋으로 갈랐다.** 실패(raise) / 데이터 없음(정상 빈 값) / 미설정(통과). 이 경계가
관념이 아니라 코드로 판별된다는 근거가 있다 — KRX 차단 시 응답은 JSON 이 아닌 HTML 이라
파싱이 예외를 던지고, 진짜 휴장일은 정상 JSON + 빈 `output` 이라 예외가 없다.

**소스가 아니라 원인으로 나눈다.** 호출자의 관심사는 "KRX 냐 DART 냐"가 아니라 "재시도해도
되나, 사람이 고쳐야 하나"다. `SourceAuthError`/`SourceQuotaError`/`SourceUnavailableError`/
`SourceSchemaError`/`SourceRequestError` 5갈래로 두고 소스는 속성(`source`)으로 싣는다.
원인별로 쿨다운이 다르다 — 인증·한도 300초, 일시 장애 60초, 스키마·요청 오류는 없음
(기다려도 해결되지 않으므로 대기가 문제를 감춘다).

**"성공"은 응답 수신이지 데이터 획득이 아니다.** 과거 구간에는 DART 미공시 종목이 많아,
"자료 없음"을 실패로 세면 정상 백테스트가 죽는다. 집계 계층은 **전량 실패일 때만** 대표
원인으로 raise 한다.

**집계 루프는 더 시도해봐야 소용없을 때 멈춘다**(`errors.stop_aggregate`). 쿨다운 중이면
자기유발 차단이 대표 원인을 오염시키고(원인이 Unavailable 이어도 Quota 로 뒤바뀐다),
쿨다운이 없는 스키마 오류는 응답 형식이 통째로 바뀐 것이라 종목별로 다를 수 없다 —
그대로 두면 포맷이 바뀐 날 200종목×3회를 다 소진한 뒤 raise 한다(일일 한도 존재).
단 한 번이라도 성공했다면 형식은 맞다는 뜻이므로 나머지는 종목별 사정으로 보고 계속
돌려 부분 결과를 지킨다.

**HTTP 계층에서 다시 뭉개지 않는다.** 전부 503 으로 내보내면 5갈래 분류를 만든 목적이
무력화된다 — 상태 코드만 보는 알림·온콜이 "외부 장애, 복구 대기"로 오판해 정작 필요한 코드
핫픽스를 미룬다. `SourceRequestError`(우리가 잘못 보냄)는 500, `SourceSchemaError`(외부는
응답했으나 해석 불가)는 502, 나머지는 503.

**미설정은 실패가 아니다 — 그래서 용도별 preflight 를 둔다.** 자격증명이 없어도 앱은 떠야
하므로 데이터 계층은 통과시키고, PIT 유니버스처럼 없으면 결과가 무의미해지는 진입점에서만
`require_krx_auth()` 로 막는다(19개월치를 다 돌기 전에). 반대로 라이브 매매 데몬처럼 인증
문제로 매매를 전면 정지시키면 안 되는 곳에는 걸지 않는다.

**저하는 유지하되 은폐만 없앴다.** 종목명·중립화 축·섹터 한도가 없어도 매매·백테스트는
성립한다. 다만 `except Exception` 을 `except DataSourceError` 로 좁혀 **우리 쪽 버그
(`TypeError` 등)는 전파**되게 하고 로그를 ERROR 로 올렸다. 이 bare except 가 이번 작업에서
가장 조용했던 실패였을 수 있다.

**작업 중 드러난 것들**(전부 코드로 확인·수정):
- 라이브 엔진과 백테스트가 같은 리스크 레이어 기능에서 **서로 다른 저하 계약**을 갖고 있었다.
  `rebalance_runner._get_sector_map` 은 bare 호출이라 섹터맵 조회 실패 시 리밸런싱 틱 전체가
  죽었다 — 선택적 리스크 한도 하나 때문에 주문을 못 내는 것은 과하다.
- 스크리너는 DART 조회 실패 시 재무 하드 필터가 후보 전원에 대해 건너뛰어지는데, 응답이
  정상 결과와 구분되지 않았다 → `financial_filter_applied` 로 드러낸다.
- **테스트가 매 실행 실제 KRX 에 로그인하고 있었다.** pykrx 는 `pykrx.website.comm` 임포트
  시점에 로그인을 시도하는 전역 부작용이 있고, 개발 컨테이너에는 시크릿이 마운트돼 있다.
  conftest 에서 자격증명을 비워 CI 와 같은 조건으로 맞췄다(실행 시간 44초 → 25초).

**검증**: 백엔드 전체 테스트 661 → **770 passed**. 모든 테스트는 실 KRX/DART/KOFIA 호출 없이
격리된다.

설계 근거: `docs/superpowers/specs/2026-08-04-external-api-silent-failure-design.md`.

**남은 한계**: ERROR 승격이 반복 호출 경로(리밸런싱일마다 부르는 중립화 조회, 실패를
캐시하지 않는 종목 카탈로그)에서 같은 사건을 여러 줄 찍는다. **억제 장치는 넣지 않기로
했다** — 이 저장소에는 로그 핸들러도 Sentry 연동도 없고 알림은 전부 명시적 `publish_alert`
호출이라, ERROR 볼륨을 소비하는 자동화가 없다. 즉 비용은 사람이 로그를 읽을 때의 가독성
뿐이고, 진행 중인 장애에서 반복 출력은 오히려 상태를 보여준다. 로그 기반 알림을 도입하는
시점에 "같은 사건의 재출력"을 낮출 장치(예: 쿨다운 단락으로 재보고된 예외 표시)를 함께
설계하는 것이 맞다.

## §49 확정 과거 데이터의 로컬 영구 저장 (완료: 2026-08-06)

백테스트 입력 6종(펀더멘털·시가총액·기간등락률/순매수·지수 및 전종목 OHLCV·PIT
지수구성·DART 재무)을 5개 정규화 테이블 + 페치 원장(`external_fetches`)에 영구
저장하고, 조회를 `cached_frame` 한 진입점으로 통일했다.

핵심은 원장이다. 정규화 테이블만으로는 "휴장일이라 0행"과 "아직 적재 안 됨"이 같은
값이라, 저장소를 붙여도 §48 이 닫으려던 조용한 실패가 그대로 재현된다.

부수 효과로 `_fetch_per_market` 의 `except Exception → 빈 프레임` 이 사라졌다.
전 시장 실패는 이제 `representative()` 대표 예외로 raise 되고, 부분 실패는 성공분을
돌려주되 확정으로 굳히지 않는다.

**남은 한계**: 최초 적재는 여전히 외부 가용성에 달려 있다. pykrx 차단 중에는 미적재
구간의 백테스트가 `DataSourceError` 로 멈춘다 — 의도한 동작이며, 조용히 빈 값으로
완주하던 이전보다 낫다.

**주의(B1, 2026-08-08 통합 리뷰)**: `stock_daily_snapshots`/`stock_period_stats` 는
PK 에 시장 구분이 없어(`(trade_date, symbol)`/`(start_date, end_date, investors,
symbol)`) KOSPI 행과 KOSDAQ 행이 같은 키공간에 섞인다. 초기 구현은 쓰기 시점에
`market` 컬럼을 채우는 곳이 `_fetch_fundamentals` 뿐이었고 읽기(`read_daily`/
`read_periods`)에 시장 필터가 없어, 전 시장을 먼저 적재한 뒤 단일시장을 조회하면
2회차(로컬 히트)부터 전 시장 결과가 섞여 나오는 회귀가 있었다(재현·수정: 같은 날
커밋). **이 저장소는 빈 상태에서 시작해야 한다** — 그 이전 스키마(시장 태깅 누락)로
이미 적재된 행이 있다면 `stock_daily_snapshots`·`stock_period_stats` 와 해당
`external_fetches` 원장 행을 지우고 재적재할 것(부분 마이그레이션 스크립트는 만들지
않았다 — 개발 DB 는 이 수정 시점에 6테이블 전부 0행이었다).

**호출자 저하 계약 정리(Task 11)**: `fetch.py` 계열이 예외를 던지게 되면서, 이를
소비하는 호출부를 세 갈래로 확정했다 — 백테스트·리밸런싱 경로(그대로 전파),
조회 화면 라우트(그대로 전파, `app/main.py` 가 500/502/503 으로 변환), 보조 지표
(개별 항목 실패는 흡수, 전량 실패는 전파). 특히 `backtests.py` 의
`_provider_with_flow` 가 `compute_flow_norm` 실패를 `flow=None` 으로 삼키던
지점(§47 사고 패턴이 재현된 진입로)을 없애고 전파로 바꿨고, `compute_flow_norm` 은
순매수가 빈 결과일 때 분모(시총/거래대금) 조회를 아예 하지 않도록 순서를 바꿨다.
`engine/rebalance_runner._is_risk_off` 는 기준지수 조회 실패 시 "위험선호로 간주해
실제 주문을 내는" 이전 동작 대신 "이번 틱은 무행동, 실패로 기록해 재시도"로
바뀌었다 — 데이터 부재를 정상으로 뭉개 실제 자금을 움직이던 §44-1/§47 최악 판본을
없앤 의도된 정책 변경이다.

**원칙 승격(I3, 2026-08-08 통합 리뷰)**: `krx_index.index_members`는 처음부터 빈
결과를 확정 기록하지 않았지만(`if codes:` 가드), 나머지 5종(펀더멘털·시가총액·
기간등락률·순매수·전종목/지수 OHLCV)이 거치는 코어 `cached_frame`은 호출자가 넘긴
`is_final`을 그대로 믿어 0행도 확정할 수 있었다 — Task 8 리뷰가 blocking 으로
지적한 것과 정확히 같은 형태가 코어에 남아 있었던 것. 이제 원칙을 코어 수준에
못박는다: **빈 결과는 "소스가 명시적으로 없다고 선언한 경우"에만 확정으로 굳힌다.**
DART(OpenDART status 013)만 이 명시적 선언에 해당해 `dart_store`가 자체 경로로
확정 기록하고, `cached_frame`을 거치는 나머지는 `row_count==0`이면 `is_final`
인자와 무관하게 항상 `final=False`로 내린다(`app/services/data/store/frame.py`).
트레이드오프는 진짜 휴장일·무실적 구간의 매 호출 재조회 비용인데, 백테스트가 거래일만
순회하는 한 실비용은 작다 — 잘못 굳혀 영구 0행으로 고착되는 쪽(§47 재발 형태)이
비교할 수 없이 위험하다.

**범위 키 소스의 한계(I4, 2026-08-08 통합 리뷰)**: `cached_frame` 의 캐시키는
`(start, end)` 문자열이라 요청 범위가 **정확히 일치**할 때만 로컬 히트한다. 그래서
지수 OHLCV·기간 통계처럼 범위로 조회하는 소스는 **선적재로 채울 수 없다.** 야간
배치가 지수 OHLCV 를 하루치(`_fetch_index_ohlcv(ymd, ymd, code)`)로 적재하고 있었는데,
실제 소비자는 전부 넓은 범위(패닉≈90영업일·섹터 252영업일·레짐 `ma_period+10`)라 키가
겹치지 않아 행만 쌓이고 게이팅에는 관여하지 못했다 — 소비자는 매번 원격을 다시 타고
같은 행을 덮어썼고, 이득 없이 실패율 분모만 늘려 알림 임계를 왜곡했다. 해당 2단계를
`_snapshot_steps` 에서 제거했다(선적재 4단계 = 펀더멘털·시가총액·전종목 OHLCV×2).
**해소(2026-08-08)**: 지수 OHLCV 에 한해 구간 커버리지 조회를 도입했다
(`index_ohlcv_coverage` 테이블 + `frame.cached_range`, 설계는
`docs/superpowers/specs/2026-08-08-index-ohlcv-coverage-design.md`). 확보 구간이
요청을 포함하면 로컬로 답하므로, 야간 배치가 400 거래일을 미리 확보해 두면 pykrx 가
막혀도 레짐·패닉·벤치마크가 굴러간다. 선적재 단계도 그래서 되살렸다.

**기간 통계(`stock_period_stats`)는 여전히 정확일치다.** 등락률·누적 순매수는 구간
자체가 값인 집계라 긴 구간에서 짧은 구간을 뽑을 수 없다 — 커버리지가 원리적으로
성립하지 않는다. 업종지수도 선적재 대상이 아니다.

**정정(2026-08-08 통합 리뷰)**: 위 문단이 "차단 시 섹터 로테이션은 멈춘다"고 적었는데
실제 동작과 다르다. `app/services/metrics/fetch.py` 의 `_fetch_index_tickers` 는
`except Exception: return []` 로 pykrx 실패를 조용히 빈 목록으로 삼키고,
`app/services/metrics/sectors.py` 의 `compute_sectors` 는 그 빈 목록으로 업종 순회
루프를 건너뛰어 `items=[]` 인 `SectorsOut` 을 예외 없이 **정상 200 응답**으로 낸다.
"멈춤"(예외로 막힘)이 아니라 **"조용한 빈 성공"**이다 — §47 이 반복 지적한 사고
형태가 이 경로에는 아직 남아 있다. `_fetch_index_tickers` 를 이 브랜치 범위에서
고치지 않았으므로(범위 밖) 후속 과제로 남긴다: `except Exception` 을 걷어내고
`DataSourceError` 를 그대로 올리거나, 최소한 호출자가 빈 목록과 조회 실패를 구분할
수 있는 신호를 돌려줘야 한다.

**섹터 로테이션의 조용한 빈 성공(2026-08-16)**: 위 문단이 "차단 시 섹터 로테이션은
멈춘다"고 암시했는데 실제 동작과 달랐다. `app/services/metrics/fetch.py` 의
`_fetch_index_tickers` 는 `except Exception: return []` 로 pykrx 실패를 조용히
빈 목록으로 삼키고, `app/services/metrics/sectors.py` 의 `compute_sectors` 는 그
빈 목록으로 업종 순회 루프를 건너뛰어 `items=[]` 인 `SectorsOut` 을 예외 없이
**정상 200 응답**으로 낸다. "멈춤"(예외로 막힘)이 아니라 **"조용한 빈 성공"**이다 —
§47 이 반복 지적한 사고 형태가 이 경로에 남아 있었다.

**해소**: `_fetch_index_tickers` 가 다른 형제 함수(`_fetch_index_ohlcv`)와
같은 패턴으로 pykrx 예외를 `SourceUnavailableError` 로 감싸 `raise` 하도록 바꿨다.
`compute_sectors` 의 시장별 순회 루프는 이 예외를 `try/except DataSourceError` 로
받아 실패한 시장만 건너뛰고 계속 진행하되(개별 업종 실패에 이미 적용 중이던 저하
계약과 동일), 루프가 끝난 뒤 **시도한 모든 시장이 조회 실패로 비었을 때만**
`representative()` 대표 예외를 올린다. 한 시장만 막히고 다른 시장이 성공하면
기존처럼 부분 결과를 돌려준다. 라우트(`api/routes/metrics.py`)는 이미
`compute_sectors` 호출을 `except Exception` 으로 감싸 503으로 변환하고 있어 별도
수정이 필요 없었다.

**`cached_range` 부분 응답 방어(2026-08-16 해소)**: 위 §11(설계서
`2026-08-08-index-ohlcv-coverage-design.md`)이 남긴 한계 — `cached_range` 가
"정상 응답 = 요청 창 전체 수신"을 검증 없이 믿어, 소스가 조용히 일부만 응답해도
요청 범위 전체를 커버로 기록하는 문제 — 를 행 수 휴리스틱으로 막았다. 반환 행의
min/max 클램프는 틀린 방향(요청은 달력일·데이터는 거래일이라 항상 과소 주장이 됨)
이라 설계서가 이미 배제했던 안이다.

구현은 계층을 나눴다: `cached_range`(frame.py, 소스 불가지) 는 `merge_coverage`
콜백에 `row_count` 를 그대로 전달만 하고 판단하지 않는다. "이 행 수가 이 구간에
그럴듯한가"는 거래일 달력이라는 도메인 지식이라 소스 쪽(`metrics/fetch.py`)에
둔다 — `app/services/market.py` 에 pykrx 호출 없는 순수 근사 함수
`estimated_trading_days(start, end)`(달력일×5/7 − 연 15일 공휴일 가정)를 추가하고,
`fetch.py` 의 `_store_merge_coverage` 가 기대 거래일 대비 수신 행 수가 절반 미만이면
(임계 `_COVERAGE_ROW_RATIO_THRESHOLD=0.5`) 커버리지 기록을 건너뛰고 경고 로그만
남긴다. 기대 거래일이 10일 미만인 짧은 구간은 근사 오차가 상대적으로 커 검사하지
않는다. 데이터 자체(`write_local`)는 이 판정과 무관하게 그대로 저장된다 — 커버리지
확정만 보류할 뿐이다. 임계값은 정밀 판정이 아니라 "요청의 절반도 못 받은" 수준의
명백한 부분 응답만 잡는 넉넉한 안전마진으로, 실측 데이터 없이 더 촘촘히 튜닝할
근거는 없다.

새로운 개선 후보가 쌓이면 이 문서에 이어서 추가한다.
