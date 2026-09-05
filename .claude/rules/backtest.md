---
paths:
  - "backend/app/services/backtest/**"
---

# 백테스트 엔진

`app/services/backtest/`

| 모듈 | 역할 |
|---|---|
| `portfolio.py` | **리밸런싱(다종목) 백테스트 코어** — `run_rebalance_backtest` |
| `signals.py` | 단일종목 기술적 신호(SMA/EMA/RSI/MACD/볼린저/돌파/모멘텀/z-score) |
| `engine.py` | 단일종목 백테스트(vectorbt) |
| `risk_caps.py` | 집중 한도·섹터 한도·변동성 타겟팅 |
| `slippage.py` / `slippage_calibration.py` | 변동성 스케일 슬리피지 |
| `attribution.py` | 팩터 IC/IR 성과귀속 |
| `fill_quality.py` | 체결품질(M1/M2/M3) 실측 |
| `tracking.py` | 실측 NAV ↔ 백테스트 기대곡선 대조 |
| `deflated_sharpe.py` | 다중검정 보정(DSR) |

## `run_rebalance_backtest` 의 구조

538줄·중첩깊이 8 로 코드베이스 최대 함수. **의도적으로 그대로 둔다** — 깊이는 패닉 오버레이
상태기계의 실제 형태이고, 분기마다 측정 근거가 주석으로 붙어 있다. 이 함수의 수치가 전략
승격의 근거라 평탄화는 리팩토링이 아니라 재작성이다(§67).

```
준비:  _adv_frame  ·  _with_sector_map  ·  _parse_panic_overlay → PanicOverlayParams(frozen)
       (순수 헬퍼로 분리됨 — 여기에 기본값·형변환·I/O 가 모여 있다)
루프:  일별 시뮬레이션
        1) 마크투마켓 + 상장폐지 write-off
        2) 전일 결정의 지연 체결(next_close)
        3) MDD 킬스위치 상태 갱신
        4) 결정 — 킬스위치 > 패닉 오버레이 > 레짐 청산 > 정기 리밸런싱
```

### 반드시 지킬 것

- **미래참조 금지.** 어떤 날 `d` 의 결정은 `panel.loc[:d]` 만 쓴다. `_targets_at`·
  `_score_factor_frame`·`_dynamic_universe` 모두 이 규약 위에 있다.
- **결정과 체결을 분리한다.** `fill_mode="next_close"`(기본)면 `d` 의 결정을 다음 거래일
  종가에 체결한다(`pending` 큐).
- **`set` 순회로 주문을 만들지 않는다.** `_apply_rebalance` 는 `sorted(set(targets)|set(val))`.
  정렬이 빠지면 실행마다 결과가 달라져 회귀 테스트 자체가 불가능해진다(§66).
- **상장폐지는 terminal gap 일 때만 확정.** 마지막 유효 관측 이후 두 번 다시 값이 없는
  경우만 write-off 한다. 거래정지 후 재개되는 interior gap 을 폐지로 오분류하면 안 된다.
  회수율 기본 0.9(종점가가 이미 최종 회수가치를 담고 있어 더 깎으면 이중 페널티).

### 파라미터 해석 규칙

`_parse_panic_overlay` 의 `_pof` 는 `or` 가 아니라 **None 검사**로 기본값을 대체한다.
`scale_in_confirm=0` 같은 "0 으로 끄기"가 유효 설정이라, `x or default` 로 쓰면 조용히
기본값이 되살아난다.

## 성과지표

`total_return, cagr, mdd, sharpe, sortino, alpha, beta, information_ratio, tracking_error,
benchmark_return, excess_return, win_rate, num_trades, num_rebalances, num_kills,
num_panic_events, factor_ic, avg_turnover, avg_turnover_actual`

- Sharpe·Sortino 는 `risk_free_rate`(연) 초과 기준.
- `avg_turnover_actual` 은 ADV 캡·정수주 절사·상하한가 체결불가 반영 **이후** 실체결 기준.

## 전략 판정 기준

- **신규 전략 검증은 반드시 PIT(생존편향 제거) KOSPI200 유니버스로.** 손질된 풀은 성과가
  붕괴한다(동적 섹터로테이션 +131% → PIT 에서 +12.9%).
- **저베타·방어형 전략은 `excess_return`/IR 이 아니라 `alpha`/Sharpe 로 판정한다.**
  강세장 구간에서는 저베타가 구조적으로 초과수익이 음수로 나온다 — 알파 소멸이 아니다.
- 현재 대표 전략 **id=23**(균형 멀티팩터, 저베타 β≈0.6·순수 알파형), 보완재 **id=24**.

## 재현성

같은 입력이면 항상 같은 수치가 나와야 한다. 검증 방법(§67):

1. 합성 패널에 전 오버레이(레짐·패닉·리스크레이어·ADV캡·상하한가·상장폐지)를 켜고
2. 결과 dict 전체(스칼라 + equity_curve/markers/holdings/trades/factor_ic 해시)를 대조

**성능 주의**: cProfile 상 시간의 75% 가 `_targets_at` 의 팩터 계산에 있다. 벡터화하면
`dropna().tail(N)`(마지막 N 개 **유효 관측**)과 패널 `tail(N)`(마지막 N **행**)이
결측 종목에서 달라져 **수치가 바뀐다**. 최적화 전에 이 차이를 먼저 결정해야 한다.
