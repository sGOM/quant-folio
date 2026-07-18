# 백엔드 서버 리팩토링 계획

작성일: 2026-07-11 · 대상: `backend/`

전반적으로 레이어 분리(web은 매매를 하지 않고 engine이 실행), `deps.py` 인증 의존성,
스키마/모델 분리는 모범적이다. 버그가 아니라 **구조 개선** 관점의 정리이며 급한 것은 없다.
아래는 임팩트 순 우선순위.

---

## 🔴 1. 엔진 러너 공통 베이스 클래스 추출 — **완료 ✅**

`engine/base_runner.py`(`BaseRunner`) 신설. `StrategyRunner`/`RebalanceRunner`가 상속하며
공통 `__init__`/`_load`/`run` 루프/`_holding_qty`/`_place`/`_position_lock`을 물려받고,
`_on_load`(추가 적재)·`_log_start`(시작 로그)·`_tick_once`(틱 본문)만 각자 구현한다.
러너별 로그 라벨/주기는 클래스 속성(`_label`/`_tick_word`/`_poll_interval`/`_tick_timeout`)으로
조정. `test_rebalance`·`test_executor` 79건 통과.

<details><summary>이전 상태(중복 표)</summary>

`engine/runner.py`의 `StrategyRunner`와 `engine/rebalance_runner.py`의 `RebalanceRunner`가
아래를 거의 그대로 중복한다.

| 중복 요소 | StrategyRunner | RebalanceRunner |
|---|---|---|
| `__init__` (strategy_id, redis, cfg, user_id, broker) | ✓ | ✓ |
| `_load()` (전략 조회→user 조회→자격 검증→broker 생성) | 거의 동일 | 거의 동일 |
| run 루프 (`wait_for` + timeout + `stop_event.wait`) | ✓ | ✓ |
| `_holding_qty`, `_place`/`execute_signal` 래핑 | ✓ | ✓ |

특히 `_load`의 "전략 없음/자격 미등록 → 경고 후 취소" 로직은 **완전히 동일**해서, 한쪽만
수정하면 다른 쪽이 어긋날 위험이 크다.

**조치**: `engine/base_runner.py`에 `BaseRunner`를 두고 `__init__`/`_load`/`run` 루프/
`_holding_qty`/`_place`/`_position_lock`을 올린다. 서브클래스는 `_on_load`(추가 적재),
`_log_start`(시작 로그), `_tick_once`(틱 본문)만 구현한다.

</details>

## 🔴 2. `app/services/metrics.py` (1061줄) 책임 과다 — **완료 ✅**

단일 모듈을 책임별 패키지 `app/services/metrics/`로 분할했다.

```
services/metrics/
  __init__.py   # 전 심볼 재노출(기존 import 경로 보존)
  common.py     # 영업일/날짜 변환, JSON-safe 숫자, MDD·변동성, _mkts, _pct_dec
  fetch.py      # _fetch_fundamentals/_market_cap/_price_change/_index_* + 펀더멘털 캐시
  factors.py    # _winsorize_zscore, _neutralize_size/_neutralize_sector, _compute_stock_scores, compute_universe_scores
  sectors.py    # compute_sectors, _compute_one_sector
  stocks.py     # compute_stocks, _compute_tech_indicators
  names.py      # _build_krx_name_map, _build_name_map
```

- 외부(라우트·엔진·스크리너·추천·백테스트·테스트)는 기존과 동일하게
  `from app.services.metrics import X` 로 접근 — `__init__.py`가 전 심볼을 재노출.
- `factors ↔ stocks` 순환(`compute_universe_scores`가 `_compute_tech_indicators` 사용,
  `compute_stocks`가 `_compute_stock_scores` 사용)은 `compute_universe_scores` 내부
  지연 import로 해소.
- 전체 테스트 223건 통과, 소비 모듈 9개 import 무결.

## 🟡 3. pykrx per-market 페칭 패턴 중복 — **완료 ✅**

`_fetch_fundamentals` / `_fetch_market_cap` / `_fetch_price_change`의 "시장 루프 →
try/except 경고 → concat, 빈 프레임 처리" 스켈레톤을 고차 헬퍼 `_fetch_per_market(
fetch_one, mkts, *, what, when, empty_columns)`로 축약했다. `fetch_one(stock, mkt)` 만
시장별 조회 로직을 담고, 반복·경고·concat·빈 프레임 처리는 헬퍼가 담당한다. 흩어진
`from pykrx import stock` 지연 임포트는 `_pykrx_stock()` 단일 진입점으로 모아 index
계열 헬퍼까지 경유하게 했다(블로킹 임포트 지연 특성 유지).

## 🟡 4. `app/services/backtest/portfolio.py` (1007줄) 백테스트 God-module — **완료 ✅**

리프 성격의 헬퍼를 책임별 하위 모듈로 분리하고, `portfolio.py`가 재노출해 기존 import
경로를 보존했다(`rebalance_runner._compute_plan`·검증 스크립트·`test_rebalance` 호환).

- `slippage.py` — `_vol_slippage_map` (`_slip`은 `_apply_rebalance` 내부 클로저라 잔류)
- `risk_caps.py` — `_cap_position_weights`, `_portfolio_vol_ann`, `_apply_risk_caps`
- `attribution.py` — `_factor_attribution`, `_risk_adjusted_metrics`, `_FACTOR_SCORE_COLS`

`_dynamic_universe`/`_regime_on_flags`/`_score_factor_frame`/`_targets_at` 등 시뮬레이션
루프와 결합도가 높은 함수는 `portfolio.py`에 유지.

## 🟢 5. 사소한 것들 — **완료 ✅**

- `_safe_float`/`_safe`/`_is_nan`/`_safe_bool`를 `app/services/_num.py` 공용 유틸로 통합.
  `metrics/common.py`·`backtest/portfolio.py`는 재노출로 기존 경로 유지, `_safe`는
  `_safe_float` 별칭으로 정리(둘 다 NaN/inf→None JSON-safe float).
- `backtests.py`의 `_fundamentals_provider_with_market_cap`은 이미
  `_fundamentals_provider`를 감싸 시총 컬럼만 덧붙이는 자연스러운 형태 — 변경 불필요(검토 완료).

---

## 진행 로그

- [x] 1. 러너 베이스 클래스 추출 (완료 — 79 테스트 통과)
- [x] 2. metrics.py 모듈 분할 (완료 — 패키지화, 223 테스트 통과)
- [x] 3. pykrx 페칭 헬퍼 (완료 — `_fetch_per_market`/`_pykrx_stock`, 223 테스트 통과)
- [x] 4. portfolio.py 모듈 분할 (완료 — slippage/risk_caps/attribution, 재노출 유지)
- [x] 5. 공용 num 유틸 (완료 — `app/services/_num.py`, backtests provider 검토 완료)
