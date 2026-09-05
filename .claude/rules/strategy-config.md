---
paths:
  - "backend/app/schemas/strategy.py"
  - "backend/engine/rebalance*.py"
  - "frontend/lib/strategy.ts"
  - "frontend/lib/api.ts"
  - "frontend/components/strategy-form/**"
---

# 전략 설정 계약 (`app/schemas/strategy.py`)

**993줄, 스키마 중 최대.** 백엔드·엔진·프론트 3자가 필드명을 정확히 맞춰야 하는 계약이다
(직렬화 경계에서 이름 변환 금지 — `docs/CONVENTIONS.md` §3).

`Strategy.config` 는 **JSONB** 라 유형·파라미터 추가에 **DB 마이그레이션이 필요 없다.**
대신 그만큼 스키마가 유일한 방어선이다.

```
백엔드  app/schemas/strategy.py   (Pydantic v2, discriminated union)
엔진    engine/rebalance.py · rebalance_runner.py · runner.py   (config dict 로 읽음)
백테스트 app/services/backtest/portfolio.py · signals.py
프론트  lib/api.ts (StrategyConfig union) · lib/strategy.ts (라벨·기본값) · components/strategy-form/
```

**필드를 추가/변경할 때 이 넷을 같은 PR에서 맞춘다.** 하나라도 빠지면 조용히 무시된다.

---

## 두 갈래 — 단일종목 vs 리밸런싱

`type` 으로 갈리는 discriminated union이다.

| 갈래 | `type` | 운용 | 러너 |
|---|---|---|---|
| **단일종목** | `sma_crossover`·`ema_crossover`·`rsi`·`macd`·`bollinger`·`breakout`·`momentum`·`zscore`·`disparity`·`donchian_squeeze`·`trix`·`obv_trend`·`atr_trailing`·`volatility_breakout`·`keltner`·`stochastic`·`custom` | 종목 1개 | `StrategyRunner` |
| **리밸런싱** | `rebalance` | 다종목 포트폴리오 | `RebalanceRunner` |

전략별 수식·금융학적 근거는 [`docs/strategies.md`](../../docs/strategies.md) 참고. 여기서는 **계약**만 다룬다.

### 단일종목 공통 (`_BaseConfig`)

모든 단일종목 유형이 상속한다.

| 필드 | 기본 | 의미 |
|---|---|---|
| `symbol` | — | KRX 6자리 코드 |
| `cash` | 10,000,000 | 초기 자본 |
| `fees` / `tax` | 0.00015 / 0.0020 | 위탁수수료(양방향) / 증권거래세(**매도 시에만**) |
| `stop_loss_pct` · `take_profit_pct` · `trailing_stop_pct` | None | 리스크 청산(None=비활성) |
| `fill_mode` | `next_close` | **익일 종가 체결이 기본** — 당일 종가 미래참조 제거. `same_close` 는 민감도 분석용 opt-in |
| `slippage_bps` / `slippage_vol_scale` | 5.0 / 0.0 | 편도 슬리피지 / 변동성 비례 스케일(0=고정) |
| `risk_free_rate` | 0.0 | Sharpe·Sortino 의 rf(연) |

`RebalanceConfig` 도 `fill_mode`·`slippage_*`·`fees`·`tax` 를 **같은 규약**으로 갖는다(상속은 아님).

---

## `RebalanceConfig` — 7개 블록의 조립

```
universe (또는 universe_rule 로 동적 후보풀)
   ↓ 사전선정
universe_rule : UniverseRule      ← 시점별 후보풀(PIT). source=KOSPI200 이면 생존편향 제거
   ↓ 최종 선정
selection     : RebalanceSelection ← method(momentum|all|score|custom)·top_n·factor_weights
                └ vol_gate : VolGate    (method=score 전용, 절대 변동성 적격 게이트)
   ↓ 비중 산정
weighting     : equal | score | inverse_vol
   ↓ 위험 통제 오버레이
risk_layer    : RiskLayer          ← 종목/섹터 집중 한도 → 변동성 타겟팅 → MDD 킬스위치
   ↓ 시장 국면 오버레이
regime_filter : RegimeFilter       ← 기준지수 MA 하회 시 현금화
panic_overlay : PanicOverlay       ← 자본항복 감지 후 재진입 가속(Arm→Confirm→Fill)
   ↓ 체결
drift_band_pct 초과 종목만 매매
```

### 블록별 요점

| 블록 | 핵심 필드 | 알아야 할 것 |
|---|---|---|
| `UniverseRule` | `source`(`fixed`\|`KOSPI200`\|`KOSPI100`\|`KRX300`), `lookback`, `pick`, `min_market_cap` | **`source` 가 지수명이면 그 시점 실제 구성종목**을 쓴다 = 생존편향 제거. 이때 `universe` 는 비워도 된다. `min_market_cap` 단위는 **억 원** |
| `RebalanceSelection` | `method`, `top_n`, `lookback`, `factor_weights`, `min_score`, `vol_gate`, `flow_window` | `score` 는 `compute_universe_scores` 를 러너/백테스트가 주입한다(스키마는 파라미터만). `custom` 은 종가 기반 지표만 허용(OHLC/거래량 불가) |
| `FactorWeights` | `momentum` .4 / `value` .3 / `lowvol` .3 / `quality` 0 / `growth` 0 | **`quality`·`growth` 는 OpenDART 키가 있어야 실제로 반영된다.** 키가 없으면 중립 0 처리 → 남은 팩터로만 점수가 나와 **편향된다** |
| `VolGate` | `spike_lookback`/`base_lookback`, `spike_min`/`spike_max`, `cap`, `require_uptrend` | 게이트로만 쓰고 **정렬 키는 바꾸지 않는다.** 데이터 부족 종목은 보수적으로 불통과. `spike_max` + 낮은 `spike_min` 이면 역방향(calm gate) |
| `RegimeFilter` | `enabled`, `index`, `ma_period`, `exit_buffer_pct`, `reentry_buffer_pct` | 두 버퍼가 **히스테리시스**다. 둘 다 0이면 무상태(구 동작). 박스권 휩쏘를 줄인다 |
| `RiskLayer` | `max_position_pct`, `max_sector_pct`, `target_vol`/`vol_lookback`/`max_leverage`, `mdd_kill_pct`/`mdd_rearm_days` | **적용 순서가 정해져 있다**: 종목 한도 → 섹터 한도 → 변동성 타겟팅 → MDD 킬. 변동성 타겟팅은 `max_leverage`(기본 1.0)로 캡되어 **디레버리징 전용** |
| `PanicOverlay` | `enabled`, `market`, `arm_level`, `arm_window`, `hold_days`, `base_exposure`/`panic_exposure`/`scale_in_confirm`, `knife_stop_pct`/`profit_reclaim_pct`, `ma_recovery_period`, `event_only` | 해석은 `_parse_panic_overlay` → `PanicOverlayParams`(frozen). **비율 0은 유효값**이라 `or` 가 아니라 None 검사로 기본값을 대체한다 |

### 발화 스케줄

| 필드 | 의미 |
|---|---|
| `cadence` | `daily`\|`weekly`\|`monthly`\|`quarterly` |
| `rebalance_weekday` (0=월~4=금) / `rebalance_dom` (1~28) | 영업일 보정은 **러너가** 수행 |
| `rebalance_time` | 기본 `"14:30"` KST(장 마감 전 체결 여유) |
| `initial_fill_immediate` | 콜드 스타트 즉시 1회 발화. 이번 주기 스케줄을 **소비**하며, 레짐 위험회피면 발동 안 함 |

### 체결 정밀도·기타

`integer_shares`(정수주 절사) · `adv_participation_cap`(유동성 참여율) ·
`price_limit_model`(호가단위+상하한가, **기본 False — 기존 결과 재현성 보존**) ·
`financial_period`(`annual` | `ttm`, **annual 유지**로 종결 §3) · `benchmark_index`(기본 KOSPI200) · `risk_free_rate`.

> **`delisting_gap_days`·`delisting_recovery` 는 스키마에 없다.** `portfolio.py` 가
> `config.get(...)` 로 읽지만 Pydantic 모델에 필드가 없고 `extra` 정책이 기본값
> (`ignore`)이라, **저장된 전략에 넣어도 조용히 버려지고 항상 기본값(10일 / 0.9)이 쓰인다.**
> 실제로 바꿀 수 있는 곳은 `run_rebalance_backtest(config=...)` 에 dict 를 직접 넘기는
> 검증 스크립트·테스트뿐이다. 전략별로 조절해야 할 일이 생기면 스키마에 먼저 추가할 것.

---

## 주의

- **`weighting="score"` 는 `selection.method="score"` 일 때만 허용**된다. `inverse_vol` 은 method 무관하게 쓸 수 있지만, 저변동 종목 비중이 `drift_band_pct` 미만이 되면 **영원히 미체결**될 수 있어 밴드를 작게(예: 0.02) 둬야 한다.
- `universe` 는 최대 300종목.
- 새 필드의 기본값은 **기존 전략의 백테스트 결과를 바꾸지 않는 값**으로 정한다(재현성 — `docs/CONVENTIONS.md` §0).
