"""id=23 의 2026-07 폭락 구간 4-arm 검증 (로드맵 §47).

financial-expert 설계. 2026-06~07 은 VKOSPI 96.94(2009년 집계 이후 최고)·이틀 연속
서킷브레이커(사상 최초)·7-31 하루 +17.91%(역대 1위) 가 겹친 구간이라, **P2 패닉
오버레이가 설계대로 작동하는지 확인할 수 있는 최상의 자연 실험**이다.

## 검증 설계
- 유니버스: PIT KOSPI200 only(손질 풀 금지).
- 4-arm: 현행(레짐+패닉) / 패닉 off / 레짐+패닉 off / KOSPI200 B&H.
- 종료일 **2026-07-30 과 07-31 양쪽 병기** — 7-31 하루가 결론을 뒤집는지 보기 위함.
- 이벤트 로그(`markers`)에서 regime_exit·panic_confirm·panic_exit_* 의 **발생 일자**를
  뽑는다. 이것이 "오버레이가 언제 껐고 언제 되샀는가"의 직접 증거다.
- 슬리피지 5/25/50bps 스윕 — VKOSPI 80~97 구간에서 5bps 편도는 과소평가다.

## 판정 규약 (중요)
- 표본이 약 22거래일·일간 변동성 6%대라 alpha 추정의 표준오차가 추정치보다 크다.
  **통계적 판정을 시도하지 않고 기술통계로만 보고한다.**
- 지수 −22% 구간에서 β0.6 포트폴리오의 excess 는 크게 (+)로 나오는데 이는 알파가
  아니라 **베타 부족의 산술적 부산물**이다(`id23-lowbeta-excess-artifact` 의 거울상).
  판정은 alpha/Sharpe 로 한다.
- **반증 조건**: (현행 − 레짐off) 차이의 50% 이상이 7-31 하루에서 발생하면 단일일
  의존이므로 **어느 결론도 채택 불가**로 보고한다.

컨테이너: docker compose exec -T web python scripts/validate_id23_crash_2026.py
"""
from __future__ import annotations

import asyncio
import copy
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
from app.services.backtest.portfolio import run_rebalance_backtest  # noqa: E402
from app.services.data.loader import (  # noqa: E402
    get_close_series,
    load_ohlcv,
    upsert_price_ticks,
)

WARMUP_START = date(2025, 1, 2)
SIM_START = date(2026, 5, 1)
END_A = date(2026, 7, 30)   # 7-31 폭등 제외
END_B = date(2026, 7, 31)   # 7-31 폭등 포함
CRASH_DAY = date(2026, 7, 31)
SLIPPAGE_BPS = [5, 25, 50]

ARM_CURRENT = "현행(레짐+패닉)"
ARM_NO_PANIC = "패닉off"
ARM_NO_BOTH = "레짐+패닉off"


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


def _arms(cfg23: dict) -> dict[str, dict]:
    """4-arm 중 백테스트로 돌리는 3개 config 를 만든다(B&H 는 벤치마크로 대체)."""
    cur = copy.deepcopy(cfg23)

    no_panic = copy.deepcopy(cfg23)
    no_panic.pop("panic_overlay", None)

    no_both = copy.deepcopy(no_panic)
    no_both.pop("regime_filter", None)

    return {ARM_CURRENT: cur, ARM_NO_PANIC: no_panic, ARM_NO_BOTH: no_both}


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
        print("⚠️ id=23 에 panic_overlay 가 설정돼 있지 않다 — 패닉 arm 은 현행과 동일해진다.",
              flush=True)

    pit_union, pool_provider = _build_pit_pool(cfg23, WARMUP_START, END_B)
    universe = pit_union if pit_union is not None else list(cfg23.get("universe", []))
    print(f"PIT union universe: {len(universe)}종목", flush=True)

    warmup_dt, end_dt = _to_dt(WARMUP_START), _to_dt(END_B, end=True)
    columns: dict[str, pd.Series] = {}
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(universe):
            series = await get_close_series(db, sym, warmup_dt, end_dt)
            if series.empty:
                try:
                    df = await asyncio.to_thread(load_ohlcv, sym, WARMUP_START, END_B)
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
        _load_regime_series, WARMUP_START, END_B, reg_ticker)
    bench_ticker = _BENCHMARK_TICKER.get(cfg23.get("benchmark_index", "KOSPI200"), "1028")
    benchmark_series = await asyncio.to_thread(
        _load_regime_series, WARMUP_START, END_B, bench_ticker)
    po = cfg23.get("panic_overlay") or {}
    panic_series = await asyncio.to_thread(
        _load_panic_series, WARMUP_START, END_B, po.get("market", "KOSPI"))
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

    # 벤치마크(B&H) — 지수 시계열로 직접 계산
    if benchmark_series is not None and not benchmark_series.empty:
        bs = benchmark_series.copy()
        bs.index = pd.to_datetime(bs.index).date
        for end_d in (END_A, END_B):
            w = bs[(pd.Index(bs.index) >= SIM_START) & (pd.Index(bs.index) <= end_d)]
            if len(w) >= 2:
                bh = float(w.iloc[-1] / w.iloc[0] - 1.0)
                print(f"[{'B&H(KOSPI200)':14}] ~{end_d}: ret {bh * 100:7.1f}%", flush=True)

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
    for name in (ARM_CURRENT, ARM_NO_PANIC, ARM_NO_BOTH):
        r = _daily_returns(curves[(name, END_B)])
        d31 = r.get(CRASH_DAY)
        print(f"  [{name:14}] 7-31 일간수익 "
              f"{d31 * 100:+.2f}%" if d31 is not None else
              f"  [{name:14}] 7-31 수익 n/a")

    a = res[(ARM_CURRENT, END_B)].get("total_return")
    b = res[(ARM_NO_BOTH, END_B)].get("total_return")
    a30 = res[(ARM_CURRENT, END_A)].get("total_return")
    b30 = res[(ARM_NO_BOTH, END_A)].get("total_return")
    if None not in (a, b, a30, b30):
        gap31, gap30 = a - b, a30 - b30
        share = abs(gap31 - gap30) / abs(gap31) if gap31 else float("inf")
        print(f"\n  (현행−레짐off) 7-30까지: {gap30 * 100:+.2f}%p")
        print(f"  (현행−레짐off) 7-31까지: {gap31 * 100:+.2f}%p")
        print(f"  → 7-31 하루가 차이에서 차지하는 비중: {share * 100:.0f}%")
        if share >= 0.5:
            print("  ⚠️ 반증 조건 충족(50% 이상) — 단일일 의존이므로 어느 결론도 채택 불가")
        else:
            print("  단일일 의존 아님 — 차이를 해석해도 된다")

    # ── 4. 슬리피지 민감도 ─────────────────────────────────────────
    print("\n=== 4. 슬리피지 민감도 (~7-31) ===", flush=True)
    for name in (ARM_CURRENT, ARM_NO_BOTH):
        for bps in SLIPPAGE_BPS:
            m, _, _ = run(arms[name], END_B, slip_bps=bps)
            print(f"  [{name:14}] {bps:>2}bps: {_fmt(m)}", flush=True)

    print("\n판정 주의: 표본 약 22거래일·일변동성 6%대 — 통계적 판정 금지, 기술통계로만 해석.")
    print("방어형은 excess/IR 이 아니라 alpha/Sharpe 로 판정한다.")


if __name__ == "__main__":
    asyncio.run(main())
