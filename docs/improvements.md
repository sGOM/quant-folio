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

새로운 개선 후보가 쌓이면 이 문서에 이어서 추가한다.
