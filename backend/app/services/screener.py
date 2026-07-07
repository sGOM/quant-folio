"""소형주 턴어라운드 스크리너 — 전 시장 스캔 + 하드 필터.

financial-expert 아키타입 ③(소형주 턴어라운드)의 하드 스크리너를 구현한다. 고정
유니버스를 점수로 랭킹하는 리밸런싱 엔진과 달리, 시장 전체에서 후보를 발굴한다:

  1. 시가총액 하위 pct(기본 20%) 소형주로 1차 축소.
  2. 유동성 바닥(min_value) + 거래대금 급증(최근5일 평균 ÷ 60일 평균 ≥ surge)으로
     '수급이 붙는' 후보만 남기고, 상위 max_candidates 로 캡(OpenDART 호출량 제한).
  3. OpenDART 재무로 하드 필터: 부채비율 ≤ max_debt(배수), 최근3년 만성적자(3년 모두
     적자) 제외. 흑자전환/실적개선·F-Score 로 종합점수를 매겨 상위 top_n 반환.

OpenDART 키가 없으면 재무 필터는 건너뛰고 수급(급증) 기준으로만 후보를 낸다
(opendart_enabled=False 로 표시). 블로킹 함수 — 호출자는 스레드풀에서 실행한다.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from app.schemas.screener import TurnaroundCandidate, TurnaroundScreenOut
from app.services.data import opendart
from app.services.metrics import (
    _approx_start,
    _build_krx_name_map,
    _build_name_map,
    _fetch_fundamentals,
    _fetch_market_cap,
    _fetch_price_change,
    _last_business_day,
    _mkts,
    _safe_float,
    _ymd,
)

logger = logging.getLogger(__name__)

# 기본 파라미터.
DEFAULT_SMALLCAP_PCT = 0.20      # 시총 하위 20%
DEFAULT_MIN_VALUE = 300_000_000  # 최소 20일 평균 거래대금 3억(유동성 바닥)
DEFAULT_SURGE = 1.5              # 거래대금 급증 임계(5일 평균/60일 평균)
DEFAULT_MAX_DEBT = 1.5          # 부채비율 상한(배수, 150%)
DEFAULT_MAX_CANDIDATES = 60      # OpenDART 재무조회 대상 상한(요율/시간 보호)


def screen_turnaround(
    as_of: date | None = None,
    market: str = "ALL",
    *,
    top_n: int = 20,
    smallcap_pct: float = DEFAULT_SMALLCAP_PCT,
    min_value: float = DEFAULT_MIN_VALUE,
    surge: float = DEFAULT_SURGE,
    max_debt: float = DEFAULT_MAX_DEBT,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> TurnaroundScreenOut:
    """소형주 턴어라운드 후보를 스크리닝한다(블로킹)."""
    if as_of is None:
        as_of = _last_business_day()
    mkts = _mkts(market)
    as_of_ymd = _ymd(as_of)

    cap_df = _fetch_market_cap(as_of_ymd, mkts)
    if cap_df.empty or "시가총액" not in cap_df.columns:
        return TurnaroundScreenOut(
            as_of=as_of, market=market, scanned=0, count=0,
            opendart_enabled=opendart.is_enabled(), items=[],
        )

    cap_df = cap_df.copy()
    cap_df.index = cap_df.index.astype(str).str.zfill(6)

    # ── 1) 시총 하위 pct 소형주 ──
    threshold = cap_df["시가총액"].quantile(smallcap_pct)
    small = cap_df[cap_df["시가총액"] <= threshold].copy()

    # ── 2) 거래대금 급증 + 유동성 ──
    pc_5d = _fetch_price_change(_ymd(_approx_start(as_of, 5, buffer=4)), as_of_ymd, mkts)
    pc_60d = _fetch_price_change(_ymd(_approx_start(as_of, 60)), as_of_ymd, mkts)
    avg5 = _period_avg_value(pc_5d, 5)
    avg60 = _period_avg_value(pc_60d, 60)

    small["avg_value_20"] = avg60.reindex(small.index)  # 60일 평균을 유동성 지표로 사용
    small["avg5"] = avg5.reindex(small.index)
    small["turnover_surge"] = small["avg5"] / small["avg_value_20"].replace(0, np.nan)

    liquid = small[
        (small["avg_value_20"].fillna(0) >= min_value)
        & (small["turnover_surge"].fillna(0) >= surge)
    ].copy()
    # 급증 강도 상위로 후보 캡(OpenDART 호출량 제한).
    liquid = liquid.sort_values("turnover_surge", ascending=False).head(max_candidates)
    candidates = list(liquid.index)
    scanned = len(candidates)

    # 밸류(PER/PBR) 참고값(있으면).
    fund = _fetch_fundamentals(as_of_ymd, mkts)
    if not fund.empty:
        fund = fund.copy()
        fund.index = fund.index.astype(str).str.zfill(6)

    # ── 3) OpenDART 재무 하드 필터 + 종합점수 ──
    qmetrics: dict = {}
    if candidates:
        try:
            qmetrics = opendart.metrics_by_symbol(candidates, as_of)
        except Exception:  # noqa: BLE001
            logger.warning("스크리너 OpenDART 조회 실패", exc_info=True)
            qmetrics = {}

    seed_names = _build_name_map()
    krx_names = _build_krx_name_map(pc_5d, pc_60d)

    items: list[TurnaroundCandidate] = []
    for code in candidates:
        surge_v = _safe_float(liquid.loc[code, "turnover_surge"])
        mcap = int(liquid.loc[code, "시가총액"]) if not _isnan(liquid.loc[code, "시가총액"]) else 0
        avgv = _safe_float(liquid.loc[code, "avg_value_20"]) or 0.0
        mkt = str(liquid.loc[code].get("시장", "")) if "시장" in liquid.columns else market

        fin = qmetrics.get(code, {})
        debt = fin.get("debt_ratio")
        loss3 = fin.get("loss_years_3")
        net = fin.get("net_income")
        turn = fin.get("turnaround")
        netg = fin.get("net_growth")
        fscore = fin.get("f_score")

        # 하드 필터(OpenDART 데이터가 있을 때만 적용) —
        #   · 부채비율 > max_debt 제외
        #   · 최근 3년 모두 적자(만성) 제외
        if opendart.is_enabled() and fin:
            if debt is not None and debt > max_debt:
                continue
            if loss3 is not None and loss3 >= 3:
                continue

        per = pbr = None
        if not fund.empty and code in fund.index:
            per = _safe_float(fund.loc[code].get("PER"))
            pbr = _safe_float(fund.loc[code].get("PBR"))

        score = _candidate_score(surge_v, turn, netg, fscore, net)
        items.append(TurnaroundCandidate(
            code=code,
            name=seed_names.get(code) or krx_names.get(code) or code,
            market=mkt or market,
            market_cap=mcap,
            avg_value_20=avgv,
            turnover_surge=surge_v,
            net_income=_safe_float(net),
            debt_ratio=_safe_float(debt),
            turnaround=_safe_float(turn),
            net_growth=_safe_float(netg),
            f_score=int(fscore) if fscore is not None else None,
            loss_years_3=int(loss3) if loss3 is not None else None,
            per=per,
            pbr=pbr,
            score=round(score, 4),
        ))

    items.sort(key=lambda c: c.score, reverse=True)
    items = items[:top_n]
    return TurnaroundScreenOut(
        as_of=as_of, market=market, scanned=scanned, count=len(items),
        opendart_enabled=opendart.is_enabled(), items=items,
    )


def _period_avg_value(pc: pd.DataFrame, days: int) -> pd.Series:
    """price_change 프레임의 기간 총거래대금 → 일평균 거래대금 Series(코드 zero-fill)."""
    if pc is None or pc.empty or "거래대금" not in pc.columns:
        return pd.Series(dtype=float)
    s = pc["거래대금"].astype(float) / float(days)
    s.index = s.index.astype(str).str.zfill(6)
    return s


def _candidate_score(surge, turn, netg, fscore, net) -> float:
    """턴어라운드 종합 후보점수. 흑자전환·실적개선·F-Score·수급급증을 가중 합산."""
    score = 0.0
    if turn:
        score += 2.0                       # 흑자전환 강한 가점
    if net is not None and net > 0:
        score += 0.5                       # 흑자 유지
    if netg is not None:
        score += max(min(netg, 2.0), -1.0)  # 순이익 YoY(과대 클립)
    if fscore is not None:
        score += fscore / 8.0              # F-Score 정규화(0~1)
    if surge is not None:
        score += min(surge, 3.0) / 3.0     # 수급 급증(0~1)
    return score


def _isnan(x) -> bool:
    try:
        return not np.isfinite(float(x))
    except (TypeError, ValueError):
        return True
