"""id=23 의 2026-07 폭락 구간 4-arm 검증 (로드맵 §47).

financial-expert 설계. 2026-06~07 은 VKOSPI 96.94(2009년 집계 이후 최고)·이틀 연속
서킷브레이커(사상 최초)·7-31 하루 +17.91%(역대 1위) 가 겹친 구간이라, 오버레이가
설계대로 작동하는지 확인할 수 있는 최상의 자연 실험이다.

## 검증 설계
- 유니버스: PIT KOSPI200 only(손질 풀 금지).
- 4-arm: **현행(레짐만) / 레짐+패닉ON / 오버레이 전무 / KOSPI200 B&H**.
  id=23 config 에는 `panic_overlay` 가 **없다**(§47 전제 정정). 따라서 '패닉 off' arm 은
  현행과 동일해져 무의미하므로, 반대로 **패닉을 켠 변형**을 arm 으로 넣어 P2 를 검증한다
  (오버레이 파라미터는 `run_panic_overlay_comparison.py` 의 대표안 A 와 동일).
  cfg 에 panic_overlay 가 이미 있으면 반대로 'off' 변형을 넣어 4-arm 을 유지한다.
- 종료일 **2026-07-30 과 07-31 양쪽 병기** — 7-31 하루가 결론을 뒤집는지 보기 위함.
- 이벤트 로그(`markers`)에서 regime_exit·panic_confirm·panic_exit_* 의 **발생 일자**를
  뽑는다. 이것이 "오버레이가 언제 껐고 언제 되샀는가"의 직접 증거다.
- 슬리피지 5/25/50bps 스윕 — VKOSPI 80~97 구간에서 5bps 편도는 과소평가다.

## 판정 규약 (중요)
- 시뮬 구간(2026-05-01~07-31)은 약 60거래일이고 폭락 국면 자체는 약 22거래일이다.
  일간 변동성 6%대라 alpha 추정의 표준오차가 추정치보다 크다 —
  **통계적 판정을 시도하지 않고 기술통계로만 보고한다.**
- 지수 −22% 구간에서 β0.6 포트폴리오의 excess 는 크게 (+)로 나오는데 이는 알파가
  아니라 **베타 부족의 산술적 부산물**이다(`id23-lowbeta-excess-artifact` 의 거울상).
  판정은 alpha/Sharpe 로 한다. B&H 도 같은 지표 집합으로 계산해 같은 잣대로 비교한다.
- **반증 조건**: (현행 − 오버레이전무) 차이의 50% 이상이 7-31 하루에서 발생하면 단일일
  의존이므로 **어느 결론도 채택 불가**로 보고한다. 단, 그 차이 자체가 `_MIN_GAP` 미만이면
  '단일일 의존'이 아니라 **차이 없음(판정 무의미)** 으로 따로 보고한다.

컨테이너: docker compose exec -T web python scripts/validate_id23_crash_2026.py
"""
from __future__ import annotations

import asyncio
import copy
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date  # noqa: E402

import pandas as pd  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402,F401  KRX_ID/PW 주입
from app.api.routes.backtests import (  # noqa: E402
    _BENCHMARK_TICKER,
    _REGIME_INDEX_TICKER,
    _build_pit_pool,
    _fundamentals_provider,
    _load_panic_series,
    _load_regime_series,
    _to_dt,
)
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models import Strategy  # noqa: E402
from app.services.backtest.attribution import _risk_adjusted_metrics  # noqa: E402
from app.services.backtest.portfolio import run_rebalance_backtest  # noqa: E402
from app.services.metrics.common import _compute_mdd  # noqa: E402
from app.services.data.loader import (  # noqa: E402
    get_close_series,
    load_ohlcv,
    upsert_price_ticks,
)

SIM_START = date(2026, 5, 1)
END_A = date(2026, 7, 30)   # 7-31 폭등 제외
END_B = date(2026, 7, 31)   # 7-31 폭등 포함
CRASH_DAY = date(2026, 7, 31)
SLIPPAGE_BPS = [5, 25, 50]

ARM_CURRENT = "현행(레짐만)"
ARM_PANIC_ON = "레짐+패닉ON"
ARM_PANIC_OFF = "패닉off"
ARM_NO_BOTH = "오버레이 전무"
ARM_BH = "B&H(KOSPI200)"

# 패닉 ON arm 의 오버레이 파라미터. `run_panic_overlay_comparison.py` 의 **대표안 A**
# (= id=23 코어 그대로 + panic_overlay 추가)와 동일하게 맞춰, 이 스크립트의 결과가 기존
# 오버레이 비교 실험과 같은 축에서 읽히게 한다. 임의로 새 값을 고르면 그 자체가
# 미검증 자유도가 된다.
PANIC_OVERLAY_ON = {
    "enabled": True, "market": "KOSPI", "arm_level": "warning", "arm_window": 5,
    "hold_days": 20, "profit_reclaim_pct": 0.5, "knife_stop_pct": 0.05,
    "base_exposure": 0.70, "panic_exposure": 1.00, "scale_in_confirm": 0.5,
    "ma_recovery_period": 20, "event_only": False,
}

# 반증 조건의 하한. arm 간 누적수익 차이가 이보다 작으면 '7-31 단일일 의존'을 따지기
# 이전에 **차이 자체가 없는** 것이므로 판정이 무의미하다. 이 하한이 없으면 gap 이 0 에
# 가까울 때 share = |Δgap|/|gap| 가 발산해 무조건 '채택 불가'로 잘못 분류된다.
_MIN_GAP = 0.005   # 0.5%p

# 워밍업 길이는 cfg 의 최장 룩백에서 **유도**한다. 고정 상수(구 2025-01-02)는 근거가
# 없는 데다, 필요 이상으로 길면 그만큼 `_build_pit_pool` 의 월별 KRX 조회가 늘어난다
# (로드맵 §44-1 의 인증 차단 원인). 월 21거래일로 환산하고 여유를 더한 뒤, 연간 팩터를
# 대비해 12개월 아래로는 줄이지 않는다.
_WARMUP_FLOOR_MONTHS = 12
_WARMUP_MARGIN_MONTHS = 2


def _n(x):
    try:
        v = float(x)
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        return None


def _fmt(m: dict) -> str:
    def g(k, pct=False, nd=2):
        v = m.get(k)
        if v is None:
            return "n/a"
        return f"{v * 100:.1f}%" if pct else f"{v:.{nd}f}"

    return (f"ret {g('total_return', True):>8} alpha {g('alpha', True):>8} "
            f"beta {g('beta'):>5} shp {g('sharpe'):>5} mdd {g('mdd', True):>7} "
            f"turn {g('avg_turnover_actual', True):>7}")


def _warmup_start(cfg: dict, sim_start: date) -> date:
    """cfg 의 최장 룩백(거래일)에서 워밍업 시작일을 유도한다.

    PIT 후보풀 조회는 워밍업 구간의 **월마다** KRX 를 부르므로(§44-1), 워밍업 1개월이
    곧 KRX 요청 수다. 근거 없는 여유는 차단 위험으로 직결되니 룩백에서 계산한다.
    """
    rf = cfg.get("regime_filter") or {}
    ur = cfg.get("universe_rule") or {}
    rl = cfg.get("risk_layer") or {}
    sel = cfg.get("selection") or {}
    max_td = max(
        int(rf.get("ma_period", 0) or 0),
        int(ur.get("lookback", 0) or 0),
        int(rl.get("vol_lookback", 0) or 0),
        int(sel.get("lookback", 0) or 0),
    )
    months = max(math.ceil(max_td / 21.0) + _WARMUP_MARGIN_MONTHS, _WARMUP_FLOOR_MONTHS)
    y, m = sim_start.year, sim_start.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _arms(cfg23: dict) -> dict[str, dict]:
    """4-arm 중 백테스트로 돌리는 3개 config 를 만든다(B&H 는 벤치마크 시계열로 대체).

    id=23 에는 `panic_overlay` 가 없으므로(§47 전제 정정) '패닉 off' 를 arm 으로 두면
    현행과 **바이트 단위로 동일한 config** 가 되어 백테스트만 낭비된다. 그래서 패닉이
    없으면 **켠 변형**을, 이미 있으면 **끈 변형**을 넣어 어느 쪽이든 4-arm 을 유지한다.
    """
    cur = copy.deepcopy(cfg23)

    no_both = copy.deepcopy(cfg23)
    no_both.pop("panic_overlay", None)
    no_both.pop("regime_filter", None)

    variant = copy.deepcopy(cfg23)
    if cfg23.get("panic_overlay"):
        variant.pop("panic_overlay", None)
        variant_name = ARM_PANIC_OFF
    else:
        variant["panic_overlay"] = copy.deepcopy(PANIC_OVERLAY_ON)
        variant_name = ARM_PANIC_ON

    return {ARM_CURRENT: cur, variant_name: variant, ARM_NO_BOTH: no_both}


def _bh_metrics(bench: pd.Series, start: date, end_d: date,
                rf_annual: float) -> tuple[dict, list]:
    """B&H(벤치마크 매수후보유)를 다른 arm 과 **같은 지표 집합**으로 계산한다.

    기존에는 total_return 만 찍어, 판정 규약이 요구하는 alpha/Sharpe·MDD 로는 비교조차
    할 수 없었다(폭락 구간 방어형 검증에서 MDD 누락은 치명적). 자기 자신을 벤치마크로
    넣으므로 beta=1·alpha=0 이 나오는 것이 정상이며, 이는 다른 arm 의 beta 를 읽는
    기준선이 된다. 거래비용은 1회 매수뿐이라 무시한다(정의상 회전율 0).
    """
    idx = pd.Index(bench.index)
    w = bench[(idx >= start) & (idx <= end_d)]
    if len(w) < 2:
        return {}, []
    r = w.pct_change().dropna().to_numpy(dtype=float)
    radj = _risk_adjusted_metrics(r, r, rf_annual)
    m = {
        "total_return": float(w.iloc[-1] / w.iloc[0] - 1.0),
        "sharpe": radj.get("sharpe"),
        "mdd": _compute_mdd(w),
        "alpha": radj.get("alpha"),
        "beta": radj.get("beta"),
        "information_ratio": radj.get("information_ratio"),
        "avg_turnover_actual": 0.0,
    }
    return m, [{"t": d, "v": float(v)} for d, v in w.items()]


def _daily_returns(equity: list) -> pd.Series:
    """equity_curve(list[{t, v}]) → 일간 수익률 Series(index=date)."""
    if not equity:
        return pd.Series(dtype=float)
    idx, vals = [], []
    for p in equity:
        t = p.get("t") if isinstance(p, dict) else None
        v = p.get("v") if isinstance(p, dict) else None
        if t is None or v is None:
            continue
        idx.append(pd.Timestamp(t).date())
        vals.append(float(v))
    s = pd.Series(vals, index=pd.Index(idx)).sort_index()
    return s.pct_change().dropna()


async def main() -> None:
    async with AsyncSessionLocal() as db:
        s = await db.scalar(select(Strategy).where(Strategy.id == 23))
        cfg23 = dict(s.config)
    print(f"id=23 cadence={cfg23.get('cadence')} "
          f"regime={bool(cfg23.get('regime_filter'))} "
          f"panic={bool(cfg23.get('panic_overlay'))}", flush=True)
    if not cfg23.get("panic_overlay"):
        print("ℹ️ id=23 에 panic_overlay 가 없다 — '패닉 off' 대신 **패닉 ON 변형**을 arm 으로 쓴다.",
              flush=True)

    warmup_start = _warmup_start(cfg23, SIM_START)
    months = (END_B.year - warmup_start.year) * 12 + END_B.month - warmup_start.month + 1
    print(f"워밍업 {warmup_start} ~ (룩백 유도) — PIT 월별 조회 {months}회 예정", flush=True)

    pit_union, pool_provider = _build_pit_pool(cfg23, warmup_start, END_B)
    universe = pit_union if pit_union is not None else list(cfg23.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(warmup_start), _to_dt(END_B, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(universe):
            series = await get_close_series(db, sym, warmup_dt, end_dt)
            if series.empty:
                try:
                    df = await asyncio.to_thread(load_ohlcv, sym, warmup_start, END_B)
                    await upsert_price_ticks(db, sym, df)
                    series = await get_close_series(db, sym, warmup_dt, end_dt)
                except Exception as e:  # noqa: BLE001
                    print(f"  {sym} 적재 실패: {e}", flush=True)
            if not series.empty:
                columns[sym] = series
            if (i + 1) % 50 == 0:
                print(f"  ...패널 {i + 1}/{len(universe)}", flush=True)
    panel = pd.DataFrame(columns)
    print(f"패널 완성: {panel.shape}", flush=True)
    if panel.empty:
        # 빈 패널로도 백테스트는 '성공'하며 수치를 낸다 — 그 수치는 전부 무의미하다.
        # KRX 로그인이 차단되면 PIT 조회가 조용히 0종목을 반환해 이 상태가 된다.
        print("❌ 패널이 비었다 — PIT 유니버스 조회 실패(KRX 인증 차단 가능성). "
              "이 상태의 결과는 신뢰할 수 없으므로 중단한다.", flush=True)
        return

    rf0 = cfg23.get("regime_filter") or {}
    reg_ticker = _REGIME_INDEX_TICKER.get(rf0.get("index", "KOSPI"), "1001")
    regime_series = await asyncio.to_thread(
        _load_regime_series, warmup_start, END_B, reg_ticker)
    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(
        _load_regime_series, warmup_start, END_B, bench_ticker)
    po = cfg23.get("panic_overlay") or {}
    panic_series = await asyncio.to_thread(
        _load_panic_series, warmup_start, END_B, po.get("market", "KOSPI"))
    print(f"적재: regime={regime_series is not None} bench={benchmark_series is not None} "
          f"panic={panic_series is not None}", flush=True)

    def run(cfg, end_d, slip_bps=None):
        c = copy.deepcopy(cfg)
        if slip_bps is not None:
            c["slippage_bps"] = slip_bps
        r = run_rebalance_backtest(
            panel, c, _to_dt(SIM_START), _to_dt(end_d, end=True),
            _fundamentals_provider, regime_series, pool_provider,
            benchmark_series, panic_series,
        )
        m = {k: _n(r.get(k)) for k in (
            "total_return", "sharpe", "mdd", "alpha", "beta",
            "information_ratio", "avg_turnover_actual",
        )}
        return m, r.get("equity_curve") or [], r.get("markers") or []

    arms = _arms(cfg23)

    # ── 1. 종료일 A/B ──────────────────────────────────────────────
    print("\n=== 1. 4-arm × 종료일 (7-30 vs 7-31) ===", flush=True)
    res: dict[tuple[str, date], dict] = {}
    curves: dict[tuple[str, date], list] = {}
    marks: dict[str, list] = {}
    for name, cfg in arms.items():
        for end_d in (END_A, END_B):
            m, eq, mk = run(cfg, end_d)
            res[(name, end_d)] = m
            curves[(name, end_d)] = eq
            if end_d == END_B:
                marks[name] = mk
            print(f"[{name:14}] ~{end_d}: {_fmt(m)}", flush=True)

    # 4번째 arm — B&H. 다른 arm 과 같은 지표 집합(alpha/Sharpe/MDD)으로 계산해
    # 판정 규약과 같은 잣대에 올린다.
    rf_annual = float(cfg23.get("risk_free_rate", 0.0) or 0.0)
    if benchmark_series is not None and not benchmark_series.empty:
        bs = benchmark_series.copy()
        bs.index = pd.to_datetime(bs.index).date
        for end_d in (END_A, END_B):
            m, eq = _bh_metrics(bs, SIM_START, end_d, rf_annual)
            if not m:
                continue
            res[(ARM_BH, end_d)] = m
            curves[(ARM_BH, end_d)] = eq
            print(f"[{ARM_BH:14}] ~{end_d}: {_fmt(m)}", flush=True)

    # ── 2. 이벤트 로그 (핵심 증거) ─────────────────────────────────
    print("\n=== 2. 오버레이 이벤트 발생 일자 (~7-31) ===", flush=True)
    for name, mk in marks.items():
        ev = [(x.get("t"), x.get("type")) for x in mk
              if x.get("type") in ("regime_exit", "mdd_exit", "panic_confirm",
                                   "panic_scale_full", "panic_exit_knife",
                                   "panic_exit_profit", "panic_exit_hold")]
        if not ev:
            print(f"  [{name}] 이벤트 없음 — 오버레이가 이 구간에서 한 번도 발동하지 않았다")
        else:
            print(f"  [{name}]")
            for t, ty in ev:
                print(f"     {t}  {ty}")

    # ── 3. 7-31 단일일 의존성 (반증 조건) ──────────────────────────
    print("\n=== 3. 7-31 단일일 의존성 검사 ===", flush=True)
    for name in list(arms) + [ARM_BH]:
        if (name, END_B) not in curves:
            continue
        r = _daily_returns(curves[(name, END_B)])
        d31 = r.get(CRASH_DAY)
        shown = f"{d31 * 100:+.2f}%" if d31 is not None else "n/a"
        print(f"  [{name:14}] 7-31 일간수익 {shown}")

    a = res[(ARM_CURRENT, END_B)].get("total_return")
    b = res[(ARM_NO_BOTH, END_B)].get("total_return")
    a30 = res[(ARM_CURRENT, END_A)].get("total_return")
    b30 = res[(ARM_NO_BOTH, END_A)].get("total_return")
    if None not in (a, b, a30, b30):
        gap31, gap30 = a - b, a30 - b30
        label = f"({ARM_CURRENT}−{ARM_NO_BOTH})"
        print(f"\n  {label} 7-30까지: {gap30 * 100:+.2f}%p")
        print(f"  {label} 7-31까지: {gap31 * 100:+.2f}%p")
        # 차이 자체가 미미하면 '단일일 의존' 판정 이전에 비교가 성립하지 않는다.
        # (하한 없이 share 를 계산하면 gap→0 에서 발산해 무조건 '채택 불가'가 된다)
        if abs(gap31) < _MIN_GAP:
            print(f"  ⚠️ arm 간 차이가 {_MIN_GAP * 100:.1f}%p 미만 — 단일일 의존 이전에 "
                  "**차이 자체가 없다**. 오버레이 효과를 논할 수 없다(판정 무의미).")
        else:
            share = abs(gap31 - gap30) / abs(gap31)
            print(f"  → 7-31 하루가 차이에서 차지하는 비중: {share * 100:.0f}%")
            if share >= 0.5:
                print("  ⚠️ 반증 조건 충족(50% 이상) — 단일일 의존이므로 어느 결론도 채택 불가")
            else:
                print("  단일일 의존 아님 — 차이를 해석해도 된다")

    # ── 4. 슬리피지 민감도 ─────────────────────────────────────────
    # B&H 는 정의상 회전율 0 이라 슬리피지 무관 — 스윕에서 제외한다.
    print("\n=== 4. 슬리피지 민감도 (~7-31) ===", flush=True)
    for name, cfg in arms.items():
        for bps in SLIPPAGE_BPS:
            m, _, _ = run(cfg, END_B, slip_bps=bps)
            print(f"  [{name:14}] {bps:>2}bps: {_fmt(m)}", flush=True)

    print("\n판정 주의: 시뮬 약 60거래일(폭락 국면 약 22거래일)·일변동성 6%대 — "
          "통계적 판정 금지, 기술통계로만 해석.")
    print("방어형은 excess/IR 이 아니라 alpha/Sharpe 로 판정한다.")


if __name__ == "__main__":
    asyncio.run(main())
