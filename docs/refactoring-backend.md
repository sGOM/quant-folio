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

## 🔴 2. `app/services/metrics.py` (1061줄) 책임 과다

pykrx 페칭 / 팩터 스코어링 / 섹터 계산 / 종목 계산 / 기술지표 / 종목명 맵을 한 파일이
전담. 다음으로 분할 제안:

```
services/metrics/
  fetch.py      # _fetch_fundamentals/_market_cap/_price_change/_index_* (pykrx)
  factors.py    # _winsorize_zscore, _neutralize_size, _compute_stock_scores
  sectors.py    # compute_sectors, _compute_one_sector
  stocks.py     # compute_stocks, _compute_tech_indicators
  names.py      # _build_krx_name_map, _build_name_map
```

> 주의: `rebalance_runner.py`가 `_approx_start`, `_fetch_index_ohlcv`, `_last_business_day`,
> `_ymd`, `compute_universe_scores`를 import 하므로 분할 시 재노출(`__init__.py`) 필요.

## 🟡 3. pykrx per-market 페칭 패턴 중복

`_fetch_fundamentals` / `_fetch_market_cap` / `_fetch_price_change`가 "시장 루프 →
try/except 경고 → concat, 빈 프레임 처리" 스켈레톤을 반복. 고차 헬퍼 `_fetch_per_market(fn, mkts, ...)`로
축약. 흩어진 `from pykrx import stock` 지연 임포트도 `fetch.py` 상단 한 곳으로 모은다.

## 🟡 4. `app/services/backtest/portfolio.py` (1007줄) 백테스트 God-module

`run_rebalance_backtest` 하나에 리밸런싱 날짜/레짐/팩터 스코어/슬리피지/리스크 캡/
팩터 귀속/리스크조정 지표가 얽힘. 최소 분리:

- `slippage.py` — `_slip`, `_vol_slippage_map`
- `risk_caps.py` — `_cap_position_weights`, `_portfolio_vol_ann`, `_apply_risk_caps`
- `attribution.py` — `_factor_attribution`, `_risk_adjusted_metrics`

> 주의: `rebalance_runner._compute_plan`이 `_dynamic_universe`, `_apply_risk_caps`를
> 런타임 import 하므로 경로 유지/재노출 필요.

## 🟢 5. 사소한 것들

- `_safe_float`/`_safe`/`_is_nan`/`_safe_bool`가 metrics.py·portfolio.py에 산재 →
  `app/services/_num.py` 공용 유틸로 통합.
- `backtests.py`의 `_fundamentals_provider` / `_fundamentals_provider_with_market_cap`은
  후자가 전자를 감싸는 형태가 자연스러운지 확인.

---

## 진행 로그

- [x] 1. 러너 베이스 클래스 추출 (완료 — 79 테스트 통과)
- [ ] 2. metrics.py 모듈 분할
- [ ] 3. pykrx 페칭 헬퍼
- [ ] 4. portfolio.py 모듈 분할
- [ ] 5. 공용 num 유틸
