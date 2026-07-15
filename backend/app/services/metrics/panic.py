"""패닉셀(자본항복·capitulation) 시장 지표 계산 — 일봉 종가·거래대금·브레드스 기반.

## 설계 개요 (financial-expert 자문 반영)
패닉셀은 단순 하락이 아니라 **투매**다. 계량금융에서 capitulation의 정의적 특징은
세 축이 동시에 나타나는 것이다: (a) 급격한 가격 하락 + (b) 거래대금 폭증 +
(c) 광범위한 참여(브레드스 붕괴). 따라서 이 지표의 핵심은 점수를 높게 주는 것이
아니라 **가격 충격 단독으로는 '패닉' 라벨을 못 달게 게이팅(gating)** 하는 것이다.
이것이 국내 시장에서 단순 조정일(-2~-3%)을 패닉으로 오탐하는 것을 막는다.

## 구성 시그널 (모두 as_of 확정 일봉 기준)
- S1 1일 급락폭       r1 = close[-1]/close[-2]-1
- S2 5일 누적 낙폭    r5 = close[-1]/close[-6]-1
- S3 60일 고점대비    dd60 = close[-1]/max(close[-60:])-1   (트리거 아님, 국면 가중)
- S4 거래대금 폭증    vr  = tv[-1]/mean(tv[-21:-1])
- S5 하락종목 비율    bdr = #(등락률<0)/#유효
- S6 급락종목 비율    cr5 = #(등락률<=-5%)/#유효  (cr10=#(<=-10%)는 하드트리거)
- S7 변동성 z-score   z   = r1/std(daily_ret[-21:-1])
- S8 종가위치(CLV)    clv = (close-low)/(high-low)   (보조, 저가중)

지수 신호(S1~S4,S7,S8)는 `_fetch_index_ohlcv` 한 번, 브레드스(S5·S6)는
`_fetch_price_change` 한 번으로 확보한다(시장당 조회 2회). 종목 단위 신저가
브레드스(S9)는 호출량이 커 후속 최적화 과제로 남겨 여기서는 계산하지 않는다.

## 한계 (UI/문서 고지 대상)
- 일봉 종가 한계: 장중 급락 후 종가 회복(V자)이면 감지 실패. "종가 확정 후 판정".
- 생존편향: `_fetch_price_change`는 현재 상장 종목 기준(과거 상폐 미포함).
- 매매 신호 아님: 자본항복은 통계적으로 바닥 근처에 발생 → 투매 동참은 최악 타이밍.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.schemas.metrics import PanicMarket, PanicOut, PanicSignal
from app.services.data.loader import bounded_socket_timeout  # pykrx 무응답 행 방지
from app.services.metrics.common import (
    _approx_start,
    _mkts,
    _prev_business_day,
    _safe_float,
    _ymd,
)
from app.services.metrics.fetch import _fetch_index_ohlcv, _fetch_price_change

logger = logging.getLogger("app.services.metrics")

# Redis 캐시 TTL (6시간 — EOD 스냅샷은 자주 변하지 않음)
PANIC_CACHE_TTL = 6 * 3600

# 시장 → pykrx 종합지수 코드
_INDEX_TICKER = {"KOSPI": "1001", "KOSDAQ": "2001"}

# ── 시장별 임계값 (경계 warn, 패닉 panic) ──────────────────────────────
# KOSDAQ은 구조적으로 변동성이 KOSPI의 약 1.3~1.4배이므로 가격/변동성 임계값을 별도로 둔다.
# 역사적 앵커: 2020-03-19, 2024-08-05(엔캐리 청산), 2008-10-24 투매일 종가 분포 기준.
_TH = {
    "KOSPI": {
        "r1": (-0.025, -0.045),
        "r5": (-0.06, -0.11),
        "z": (-2.5, -3.5),
        "hard_r1": -0.06,   # CB(-8%) 안전마진 둔 하드트리거
    },
    "KOSDAQ": {
        "r1": (-0.032, -0.06),
        "r5": (-0.08, -0.14),
        "z": (-2.5, -3.5),
        "hard_r1": -0.075,
    },
}
# 시장 공통 임계값 (참여·브레드스는 비율이라 시장 무관)
_TH_COMMON = {
    "vr": (1.5, 2.0),      # 거래대금 폭증 배율
    "bdr": (0.72, 0.85),   # 하락종목 비율
    "cr5": (0.15, 0.30),   # -5% 이하 급락종목 비율
    "cr10": (0.05, 0.10),  # -10% 이하 폭락종목 비율(하드트리거 0.10)
    "clv": (0.40, 0.15),   # 종가위치: 낮을수록(저가마감) 패닉 방향
}

# ── 종합점수 가중 (합계 100) ──
# 가격축 35(S1 20 + S2 15) · 참여 20(S4) · 브레드스 30(S5 15 + S6 15) · 변동성 10(S7) · 종가 5(S8)
_W = {"s1": 20.0, "s2": 15.0, "s4": 20.0, "s5": 15.0, "s6": 15.0, "s7": 10.0, "s8": 5.0}

# 라벨 임계
_SCORE_CAUTION = 35.0   # 주의 진입
_SCORE_WARNING = 60.0   # 경계/패닉 승격 하한
_GATE_SUB = 60.0        # 게이팅용 축 서브스코어 하한


def _ramp(x: float | None, warn: float, panic: float) -> float | None:
    """경계(warn)=0점, 패닉(panic)=100점으로 선형 보간 후 [0,100] 클램프.

    방향과 무관하게 warn=0점·panic=100점 관계만 지키면 동일 식이 성립한다
    (하락 신호는 warn>panic 둘 다 음수, 상승-악화 신호는 warn<panic).
    """
    if x is None or np.isnan(x) or panic == warn:
        return None
    v = (x - warn) / (panic - warn)
    return float(max(0.0, min(1.0, v)) * 100.0)


def _wavg(pairs: list[tuple[float, float | None]]) -> float | None:
    """(가중, 서브스코어) 목록에서 유효한 서브스코어만으로 가중평균을 낸다.

    일부 신호가 데이터 부족으로 None이면 그 항목을 빼고 남은 것으로 정규화한다.
    """
    num = sum(w * s for w, s in pairs if s is not None)
    den = sum(w for w, s in pairs if s is not None)
    return num / den if den > 0 else None


def compute_panic(market: str, as_of: date) -> PanicOut:
    """패닉셀 시장 지표를 시장(KOSPI/KOSDAQ)별로 계산해 반환한다.

    동기(블로킹) 함수이므로 호출자는 asyncio.to_thread로 실행해야 한다.
    market=ALL이면 KOSPI·KOSDAQ 둘 다, 그 외엔 해당 시장만 산출한다.
    """
    as_of_ymd = _ymd(as_of)
    prev_day_ymd = _ymd(_prev_business_day(as_of))
    # 60일 고점 + 20일 std/거래대금 기준선 확보 위해 약 90영업일치 조회
    hist_start_ymd = _ymd(_approx_start(as_of, 90, buffer=20))

    items: list[PanicMarket] = []
    unavailable: list[str] = []
    for mkt in _mkts(market):
        ticker = _INDEX_TICKER.get(mkt)
        if ticker is None:
            continue
        try:
            item = _compute_one_market(mkt, ticker, as_of_ymd, prev_day_ymd, hist_start_ymd)
        except Exception:
            logger.warning("패닉 지표 계산 오류 (market=%s)", mkt, exc_info=True)
            item = None
        if item is not None:
            items.append(item)
        else:
            # 조회/데이터 부족으로 계산 불가한 시장은 조용히 빠지지 않고 명시한다.
            # (대시보드 사용자가 "이상 없음"으로 오인하는 것을 방지)
            unavailable.append(mkt)

    return PanicOut(as_of=as_of, items=items, unavailable=unavailable)


def _index_signals(df: pd.DataFrame) -> dict:
    """지수 OHLCV 슬라이스(마지막 행=as_of)에서 가격·변동성 파생 신호를 계산한다.

    compute_panic(단일 시점)과 compute_panic_series(롤링, P2 배선)가 공유하는 핵심
    산식이다. df 는 as_of 까지의 데이터만 포함해야 한다(미래참조 방지는 호출자 책임).
    """
    if df is None or "close" not in df.columns or len(df) < 6:
        return {}
    c = df["close"].astype(float)

    r1 = _safe_float(c.iloc[-1] / c.iloc[-2] - 1)
    r5 = _safe_float(c.iloc[-1] / c.iloc[-6] - 1) if len(c) >= 6 else None

    dd60: float | None = None
    if len(c) >= 2:
        hi = c.tail(60).max()
        if hi > 0:
            dd60 = _safe_float(c.iloc[-1] / hi - 1)

    vr: float | None = None
    if "trading_value" in df.columns and len(df) >= 22:
        tv = df["trading_value"].astype(float)
        base = tv.iloc[-21:-1].mean()
        if base and base > 0:
            vr = _safe_float(tv.iloc[-1] / base)

    z: float | None = None
    if len(c) >= 22:
        daily_ret = c.pct_change()
        std20 = daily_ret.iloc[-21:-1].std()
        if std20 and std20 > 1e-9 and r1 is not None:
            z = _safe_float(r1 / std20)

    clv: float | None = None
    if {"high", "low"}.issubset(df.columns):
        hi_d = float(df["high"].iloc[-1])
        lo_d = float(df["low"].iloc[-1])
        rng = hi_d - lo_d
        if rng > 0:
            clv = _safe_float((c.iloc[-1] - lo_d) / rng)

    close = float(c.iloc[-1])
    low = float(df["low"].iloc[-1]) if "low" in df.columns else close
    return dict(r1=r1, r5=r5, dd60=dd60, vr=vr, z=z, clv=clv, close=close, low=low)


def _breadth_signals(pc: pd.DataFrame | None) -> dict:
    """등락률 프레임(1일 구간, `_fetch_price_change`)에서 브레드스 신호를 계산한다."""
    bdr = cr5 = cr10 = None
    universe = 0
    if pc is not None and not pc.empty and "등락률" in pc.columns:
        chg = pc["등락률"]  # pykrx 원값(%)
        # 거래정지/무거래(거래량 0)는 분모에서 제외
        if "거래량" in pc.columns:
            valid = chg[(pc["거래량"] > 0) & chg.notna()]
        else:
            valid = chg[chg.notna()]
        universe = int(len(valid))
        if universe > 0:
            bdr = _safe_float((valid < 0).sum() / universe)
            cr5 = _safe_float((valid <= -5.0).sum() / universe)
            cr10 = _safe_float((valid <= -10.0).sum() / universe)
    return dict(bdr=bdr, cr5=cr5, cr10=cr10, universe=universe)


def _score_and_label(mkt: str, sig: dict) -> dict:
    """지수+브레드스 신호(dict)로 종합점수·라벨·게이팅·하드트리거를 산출한다(공통 로직)."""
    th = _TH[mkt]
    r1, r5, dd60 = sig.get("r1"), sig.get("r5"), sig.get("dd60")
    vr, z, clv = sig.get("vr"), sig.get("z"), sig.get("clv")
    bdr, cr5, cr10 = sig.get("bdr"), sig.get("cr5"), sig.get("cr10")

    s1 = _ramp(r1, *th["r1"])
    s2 = _ramp(r5, *th["r5"])
    s4 = _ramp(vr, *_TH_COMMON["vr"])
    s5 = _ramp(bdr, *_TH_COMMON["bdr"])
    s6_cr5 = _ramp(cr5, *_TH_COMMON["cr5"])
    s6_cr10 = _ramp(cr10, *_TH_COMMON["cr10"])
    s6 = _max_opt(s6_cr5, s6_cr10)
    s7 = _ramp(z, *th["z"])
    s8 = _ramp(clv, *_TH_COMMON["clv"])

    weighted = [
        (_W["s1"], s1), (_W["s2"], s2), (_W["s4"], s4),
        (_W["s5"], s5), (_W["s6"], s6), (_W["s7"], s7), (_W["s8"], s8),
    ]
    avail_w = sum(w for w, s in weighted if s is not None)
    score_raw = (
        sum(w * s for w, s in weighted if s is not None) / avail_w
        if avail_w > 0 else 0.0
    )
    mult = 1.0
    if dd60 is not None:
        if dd60 <= -0.10:
            mult = 1.10
        elif dd60 > -0.03:
            mult = 0.90
    score = float(min(100.0, max(0.0, score_raw * mult)))

    price_sub = _wavg([(_W["s1"], s1), (_W["s2"], s2)])
    breadth_sub = _wavg([(_W["s5"], s5), (_W["s6"], s6)])

    gated = (
        price_sub is not None and price_sub >= _GATE_SUB
        and (
            (vr is not None and vr >= _TH_COMMON["vr"][0])
            or (breadth_sub is not None and breadth_sub >= _GATE_SUB)
        )
    )

    hard_trigger = bool(
        (r1 is not None and r1 <= th["hard_r1"])
        or (cr10 is not None and cr10 >= 0.10)
        or (bdr is not None and vr is not None and bdr >= 0.92 and vr >= 2.0)
    )
    if hard_trigger:
        score = max(score, _SCORE_WARNING)

    if hard_trigger or (score >= _SCORE_WARNING and gated):
        level = "panic"
    elif score >= _SCORE_WARNING:
        level = "warning"
    elif score >= _SCORE_CAUTION:
        level = "caution"
    else:
        level = "normal"

    return dict(
        score=score, level=level, gated=gated, hard_trigger=hard_trigger,
        price_sub=price_sub, breadth_sub=breadth_sub,
        s1=s1, s2=s2, s4=s4, s5=s5, s6=s6, s6_cr10=s6_cr10, s7=s7, s8=s8,
    )


def _compute_one_market(
    mkt: str,
    ticker: str,
    as_of_ymd: str,
    prev_day_ymd: str,
    hist_start_ymd: str,
) -> PanicMarket | None:
    """시장 1개(KOSPI 또는 KOSDAQ)의 패닉 지표를 계산한다."""
    df = _fetch_index_ohlcv(hist_start_ymd, as_of_ymd, ticker)
    if df is None or "close" not in df.columns or len(df) < 6:
        logger.warning("패닉 지표: 지수 OHLCV 부족 (market=%s)", mkt)
        return None

    sig = _index_signals(df)

    # 패닉 지표는 시장 스트레스 국면에 실행될 가능성이 높아 pykrx 무응답 시 스레드가
    # 오래 블로킹될 수 있다 → 소켓 타임아웃으로 상한을 둔다.
    try:
        with bounded_socket_timeout(20):
            pc = _fetch_price_change(prev_day_ymd, as_of_ymd, [mkt])
    except Exception:
        logger.warning("패닉 지표: 브레드스 조회 예외 (market=%s)", mkt, exc_info=True)
        pc = None
    if pc is None or pc.empty or "등락률" not in pc.columns:
        logger.warning("패닉 지표: 브레드스 조회 실패 (market=%s)", mkt)
    breadth = _breadth_signals(pc)
    sig.update(breadth)

    res = _score_and_label(mkt, sig)

    signals = [
        PanicSignal(key="r1", label="1일 급락", value=sig.get("r1"), subscore=res["s1"],
                    weight=_W["s1"], warn=_TH[mkt]["r1"][0], panic=_TH[mkt]["r1"][1]),
        PanicSignal(key="r5", label="5일 누적낙폭", value=sig.get("r5"), subscore=res["s2"],
                    weight=_W["s2"], warn=_TH[mkt]["r5"][0], panic=_TH[mkt]["r5"][1]),
        PanicSignal(key="vr", label="거래대금 폭증", value=sig.get("vr"), subscore=res["s4"],
                    weight=_W["s4"], warn=_TH_COMMON["vr"][0], panic=_TH_COMMON["vr"][1]),
        PanicSignal(key="bdr", label="하락종목 비율", value=sig.get("bdr"), subscore=res["s5"],
                    weight=_W["s5"], warn=_TH_COMMON["bdr"][0], panic=_TH_COMMON["bdr"][1]),
        PanicSignal(key="cr5", label="급락종목 비율(-5%)", value=sig.get("cr5"), subscore=res["s6"],
                    weight=_W["s6"], warn=_TH_COMMON["cr5"][0], panic=_TH_COMMON["cr5"][1]),
        PanicSignal(key="z", label="변동성 z-score", value=sig.get("z"), subscore=res["s7"],
                    weight=_W["s7"], warn=_TH[mkt]["z"][0], panic=_TH[mkt]["z"][1]),
        PanicSignal(key="clv", label="종가위치(CLV)", value=sig.get("clv"), subscore=res["s8"],
                    weight=_W["s8"], warn=_TH_COMMON["clv"][0], panic=_TH_COMMON["clv"][1]),
        # 비가중 참고 신호(하드트리거·국면)
        PanicSignal(key="cr10", label="폭락종목 비율(-10%)", value=sig.get("cr10"), subscore=res["s6_cr10"],
                    weight=0.0, warn=_TH_COMMON["cr10"][0], panic=_TH_COMMON["cr10"][1]),
        PanicSignal(key="dd60", label="60일 고점대비", value=sig.get("dd60"), subscore=None,
                    weight=0.0, warn=-0.03, panic=-0.10),
    ]

    return PanicMarket(
        market=mkt,
        score=res["score"],
        level=res["level"],
        gated=res["gated"],
        hard_trigger=res["hard_trigger"],
        price_sub=res["price_sub"],
        breadth_sub=res["breadth_sub"],
        dd60=sig.get("dd60"),
        universe=breadth["universe"],
        signals=signals,
    )


# ═══════════════════════ 백테스트용 롤링 시계열(P2 배선) ═══════════════════════

# 브레드스(등락종목비율) 로컬 캐시 디렉터리. pykrx get_market_price_change 는 날짜쌍
# 단위 조회만 지원해(시계열 벌크 엔드포인트 없음) 백테스트 구간 전체를 계산하려면
# 거래일 수만큼 호출이 필요하다 — 재실행 시 재조회를 피하려고 파일로 영속화한다.
_BREADTH_CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache"

# 롤링 시계열에서 매 거래일 _index_signals 에 넘길 과거 윈도우 길이. _index_signals 가
# 참조하는 최장 과거는 dd60(60봉)이므로 그보다 넉넉히 잡으면(65) 전체 이력을 넘길
# 때와 결과가 동일하면서 매 반복 비용이 O(창길이)로 상한 → 전체 O(n²)→O(n).
_SERIES_LOOKBACK = 65


def _breadth_cache_path(mkt: str, cache_dir: str | None) -> Path:
    base = Path(cache_dir) if cache_dir else _BREADTH_CACHE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"panic_breadth_{mkt.upper()}.json"


def _load_breadth_cache(mkt: str, cache_dir: str | None) -> dict[str, dict]:
    path = _breadth_cache_path(mkt, cache_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("패닉 브레드스 캐시 로드 실패 (%s)", path, exc_info=True)
        return {}


def _save_breadth_cache(mkt: str, cache_dir: str | None, cache: dict[str, dict]) -> None:
    path = _breadth_cache_path(mkt, cache_dir)
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("패닉 브레드스 캐시 저장 실패 (%s)", path, exc_info=True)


def compute_panic_series(
    market: str,
    start: date,
    end: date,
    *,
    cache_dir: str | None = None,
    progress_every: int | None = None,
) -> pd.DataFrame:
    """백테스트용 패닉 지표 롤링 시계열 — [start, end] 구간 각 거래일의 확정 지표.

    `compute_panic(market, as_of=d)` 를 매 거래일 롤링 호출하는 것과 동일한 정의이되
    (각 날짜 d 의 값은 d 까지의 확정 데이터만 사용 — 미래참조 없음), 지수 OHLCV 는
    구간 전체를 한 번만 조회해 확장윈도우(expanding window)로 재계산함으로써 지수쪽
    네트워크 호출을 1회로 줄인다. 브레드스(하락종목비율 등)는 pykrx 특성상 날짜쌍
    단위 조회만 가능해 거래일마다 호출이 필요하므로, 로컬 파일 캐시로 재실행 시
    재조회를 피한다(최초 실행은 구간 거래일 수만큼 호출 — 느릴 수 있음).

    동기(블로킹) 함수이므로 호출자는 스레드풀에서 실행해야 한다.

    :return: index=날짜(정규화, tz-naive), columns=[score, level, gated, hard_trigger,
        close, low, dd60]. 지수 데이터가 부족하면 빈 DataFrame.
    """
    mkt = market.upper()
    ticker = _INDEX_TICKER.get(mkt)
    if ticker is None:
        return pd.DataFrame()

    hist_start_ymd = _ymd(_approx_start(start, 90, buffer=30))
    end_ymd = _ymd(end)
    df = _fetch_index_ohlcv(hist_start_ymd, end_ymd, ticker)
    if df is None or "close" not in df.columns or len(df) < 6:
        logger.warning("패닉 시계열: 지수 OHLCV 부족 (market=%s)", mkt)
        return pd.DataFrame()

    df = df.sort_index()
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = pd.DatetimeIndex(idx).normalize()
    df = df[~df.index.duplicated(keep="last")]

    cache = _load_breadth_cache(mkt, cache_dir)
    cache_dirty = False
    rows: list[dict] = []
    n_days = len(df)
    fetched = 0
    for i in range(5, n_days):
        d = df.index[i]
        if d.date() < start or d.date() > end:
            continue
        window = df.iloc[max(0, i - _SERIES_LOOKBACK + 1) : i + 1]
        sig = _index_signals(window)
        if not sig:
            continue
        d_ymd = _ymd(d.date())
        cached = cache.get(d_ymd)
        if cached is None:
            prev_ymd = _ymd(_prev_business_day(d.date()))
            try:
                with bounded_socket_timeout(20):
                    pc = _fetch_price_change(prev_ymd, d_ymd, [mkt])
            except Exception:
                logger.warning("패닉 시계열: 브레드스 조회 실패 (%s)", d_ymd, exc_info=True)
                pc = None
            breadth = _breadth_signals(pc)
            fetched += 1
            # 조회 실패(universe=0)는 캐시에 굳히지 않는다 — 굳히면 일시적 네트워크·세션
            # 실패가 그날 브레드스를 영구 결측으로 만들어(자가복구 불가) 패닉 점수를
            # 지수신호만으로 저평가시킨다. 유효 표본일 때만 저장하고, 실패일은 다음
            # 실행에서 재시도한다(거래일이면 브레드스 universe 는 항상 >0).
            if breadth.get("universe", 0) > 0:
                cache[d_ymd] = breadth
                cache_dirty = True
                # 장시간(수십 분) 소요될 수 있는 구간이므로 중간 실패(세션 만료·네트워크
                # 단절)로 앞선 조회가 전부 유실되지 않도록 일정 주기로 캐시를 중간 저장한다.
                if len(cache) % 20 == 0:
                    _save_breadth_cache(mkt, cache_dir, cache)
                    cache_dirty = False
            if progress_every and fetched % progress_every == 0:
                logger.info("패닉 시계열 진행: %s (%d/%d)", d_ymd, i, n_days)
        else:
            breadth = cached
        sig.update(breadth)
        res = _score_and_label(mkt, sig)
        rows.append({
            "date": d,
            "score": res["score"],
            "level": res["level"],
            "gated": res["gated"],
            "hard_trigger": res["hard_trigger"],
            "close": sig.get("close"),
            "low": sig.get("low"),
            "dd60": sig.get("dd60"),
        })

    if cache_dirty:
        _save_breadth_cache(mkt, cache_dir, cache)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).set_index("date").sort_index()
    return out


def _max_opt(a: float | None, b: float | None) -> float | None:
    """None을 허용하는 max. 둘 다 None이면 None."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
