# 06. 백테스팅과 신호 로직

이 문서의 목표: 과거 데이터로 전략을 검증하는 **백테스트 경로**와, 백테스트·실거래가
공유하는 **신호 생성 로직**을 이해하는 것. 금융 도메인 지식은 [07-glossary.md](07-glossary.md)
를 곁들여 보면 좋다.

---

## 1. 백테스트란 무엇인가 (개발자용 정의)

> 전략을 **과거 시세에 그대로 적용**해봤을 때 수익률·낙폭(MDD)·샤프지수 등이
> 어땠는지 시뮬레이션하는 것.

"이 전략으로 작년에 돌렸으면 얼마 벌었을까?" 를 계산한다. 단위 테스트가 코드의
정확성을 검증하듯, 백테스트는 **전략의 수익성**을 검증한다.

---

## 2. 백테스트 요청의 흐름 (`routes/backtests.py`)

```
POST /api/strategies/{id}/backtest  { period_start, period_end }
 ├─ ① price_ticks(DB)에서 해당 기간 시세 조회
 │     없으면 → load_ohlcv(FinanceDataReader)로 적재 후 재조회
 │     (한 번 적재하면 price_ticks 가 단일 출처가 됨)
 ├─ ② run_backtest(series, config)  ← CPU 바운드!
 │     → await run_in_threadpool(...)  로 스레드풀에서 실행(이벤트 루프 보호)
 └─ ③ 결과를 backtests 테이블에 저장 + 전략 status=BACKTESTED
```

**왜 스레드풀?** vectorbt 계산은 수백 ms~수 초가 걸리는 동기 CPU 작업이다. 이걸
이벤트 루프에서 그냥 돌리면 그동안 **다른 모든 요청이 멈춘다**. 그래서 별도 스레드로
던진다(Spring 의 `@Async` 와 같은 동기).

---

## 3. 백테스트 엔진 (`services/backtest/engine.py`)

[vectorbt](https://vectorbt.dev/) 라이브러리로 신호→성과를 벡터연산한다.

```python
entries, exits = generate_signals(signal_input, config)   # 매수/매도 시점 bool 시계열
pf = vbt.Portfolio.from_signals(
    close, entries, exits, init_cash=cash, fees=effective_fees, freq="1D",
    sl_stop=..., sl_trail=..., tp_stop=...)                # 손절/익절/트레일링 반영
return {
    "total_return": pf.total_return(),   # 총수익률
    "mdd": pf.max_drawdown(),            # 최대낙폭
    "sharpe": pf.sharpe_ratio(),         # 위험대비수익
    "win_rate": ..., "num_trades": ...,
    "equity_curve": [...],               # 자산곡선(차트용)
    "markers": [...],                    # 매매 시점 마커(차트용)
}
```

### 거래비용을 정확히 반영하는 트릭

KRX 실거래엔 **위탁수수료(양방향)** 와 **증권거래세(매도 시에만, 2026년 0.20%)** 가 붙는다.
그런데 vectorbt 의 `fees` 는 매수·매도에 **대칭**으로만 적용된다(매도 전용 세율을 못 받음).
그래서:

```python
effective_fees = fees + tax / 2.0
```

세금을 양방향에 절반씩 나눠 실효 수수료로 환산 → **1회 왕복(매수+매도) 총비용이
`2*fees + tax` 로 정확히 일치**한다. (비용을 무시하면 백테스트가 비현실적으로 좋게 나옴)

---

## 4. 신호 생성 — 백테스트와 실거래의 심장 (`services/backtest/signals.py`)

이 파일이 **이 프로젝트에서 가장 중요한 순수 로직**이다. 백테스트도, 실시간 엔진도
**모두 이 함수들을 호출**한다 → 그래서 둘의 결과가 일관된다.

```python
def generate_signals(data, config) -> (entries, exits):  # 백테스트가 호출
def latest_signal(data, config)   -> "buy"|"sell"|None:  # 실시간 엔진이 호출
                                                          #   (내부에서 generate_signals)
```

### 전략 유형 디스패처

config 의 `"type"` 으로 알맞은 신호 생성기를 고른다. 두 부류:

- **close-only**(종가만 필요): `sma_crossover`, `ema_crossover`, `rsi`, `macd`,
  `bollinger`, `breakout`, `momentum`, `zscore`, `disparity`, `donchian_squeeze`, `trix`
- **OHLC**(고가/저가/거래량 필요): `atr_trailing`, `volatility_breakout`, `keltner`,
  `stochastic`, `obv_trend`
- **custom**: 사용자가 UI 에서 조립한 AND/OR 규칙 트리

`requires_ohlc(config)` 가 어느 입력(종가 Series vs OHLCV DataFrame)이 필요한지
판단해 데이터 로딩 경로를 분기한다.

### "크로스오버 이벤트" 규약

모든 신호는 **조건이 거짓→참으로 바뀌는 순간**(상승 에지)만 잡는다:

```python
def _cross_up(a, b):   # a 가 b 를 상향 돌파하는 그 봉만 True
    return (a > b) & (a.shift(1) <= b.shift(1))
```

예: SMA 골든크로스는 단순이동평균선이 교차하는 **그 봉**에서만 매수 신호가 한 번
뜬다. 안 그러면 "단기선 > 장기선" 인 동안 매일 매수 신호가 떠 중복 주문이 난다.

---

## 5. ⚠️ 미래참조(look-ahead bias) — 백테스트의 1급 함정

> **미래참조**: 그 시점엔 알 수 없었던 미래 정보를 신호 계산에 쓰는 실수.
> 백테스트 성과를 **비현실적으로 좋게** 만들고 실거래에서 무너진다.

이 코드베이스는 미래참조를 **집요하게** 막는다. 예시:

```python
# breakout: 당일 종가를 채널 계산에서 제외(shift(1))해야
#           "오늘 종가로 만든 채널을 오늘 돌파" 하는 모순을 막는다
upper = close.rolling(period).max().shift(1)   # ← shift(1) 이 핵심
entries = (close > upper) & (close.shift(1) <= upper)
```

`signals.py` 주석을 보면 거의 모든 전략에 **"미래참조 처리:"** 설명이 달려 있다.
규약은 일관된다: **"t 시점 신호는 t 종가가 확정된 뒤 계산되고, t 종가로 체결"**.
채널/돌파처럼 과거 레벨을 봐야 하는 건 `shift(1)` 로 당일 봉을 제외한다.

이건 단순 코딩 디테일이 아니라 **백테스트의 신뢰성 그 자체**다. 신호를 수정할 땐
반드시 "이 계산에 미래 정보가 새지 않나?" 를 자문해야 한다.

---

## 6. 실거래에서 같은 신호를 쓰는 법 (다시 runner)

[05 문서](05-trading-engine.md)에서 본 `_tick()` 을 떠올리면:

```python
series.loc[today] = 현재가          # 오늘 봉을 실시간 현재가로 채우고
sig = latest_signal(series, cfg)    # 백테스트와 같은 함수로 마지막 봉 신호를 본다
```

즉 실거래는 "과거 일봉 + 오늘 현재가" 로 시계열을 만들어 **백테스트와 동일한 신호
함수**를 돌린다. 백테스트에서 검증한 전략이 실거래에서 **다르게 행동하지 않도록**
하는 설계의 정수다.

> OHLC 전략의 경우 러너는 일중 고저를 정밀 추적하지 않으므로, 오늘 봉의 high/low 를
> 현재가로 보수적으로 근사한다(주석 참고). 신호의 핵심은 종가 기준이고 ATR/채널은
> 과거 봉 비중이 커서 실용상 충분하다.

---

## 7. custom 전략 (비주얼 룰 빌더)

사용자가 UI 에서 "RSI < 30 AND 종가 > SMA20" 같은 규칙을 조립하면, 그게 JSON
트리(`entry`/`exit`, AND/OR 그룹)로 저장된다. `_custom_signals` 가 이 트리를 평가해
상승 에지를 신호로 변환한다. 워밍업(지표 NaN) 구간의 가짜 신호를 막는 **유효성 추적**
(`valid` Series)이 들어 있는 게 포인트.

---

## 다음 단계

도메인 용어가 낯설면 용어 사전을 보자.

→ [07-glossary.md](07-glossary.md)

### 직접 열어볼 파일
- `backend/app/services/backtest/signals.py` — 길지만 한 함수씩 읽으면 쉽다.
  `_cross_up`/`_cross_down` 과 `_sma_signals` 부터.
- `backend/app/services/backtest/engine.py` — 100줄. vectorbt 호출부.
- `backend/app/api/routes/backtests.py` — 요청 흐름.
- `docs/strategies.md` — 각 전략의 금융학적 근거·수식(매우 상세).
