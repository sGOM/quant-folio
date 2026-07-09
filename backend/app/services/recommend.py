"""KOSPI200 팩터 가중 추천 서비스 — 현 시점 구성종목 멀티팩터 스코어링.

'추천' 화면 전용. 사용자가 팩터 비중(모멘텀·밸류·저변동·퀄리티·성장)을 조절해 Top-N을
뽑는 UX 를 지원한다. 핵심 설계는 **가중치와 무관한 팩터별 세부점수를 as_of 당 1회만
계산해 내려주는 것**이다:

  - `_compute_stock_scores` 는 각 카테고리 z-score(score_momentum/value/lowvol/quality/
    growth)를 가중치와 독립적으로 산출하고 마지막에만 가중합한다. 따라서 세부점수만
    캐시해 두면, 사용자가 슬라이더로 비중을 바꿀 때마다 프론트에서 가중합·정렬·Top-N 을
    즉시 재계산할 수 있다(무거운 KRX/OpenDART 재조회 불필요).

크로스섹션 한정: z-score 는 **KOSPI200 구성종목 집합 안에서의 상대 우위**로 계산된다
(전 시장이 아니라). "코스피200 중 상위"라는 화면 의미와 일치한다.

블로킹(sync) 함수 — 호출부(라우트)가 asyncio.to_thread 로 실행한다. compute_stocks 와
동일한 pykrx 일괄조회 헬퍼를 재사용하되, 후보를 KOSPI200 멤버로 한정한다.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from app.schemas.recommend import RecommendMember, RecommendOut
from app.services.data import krx_index, opendart
from app.services.metrics import (
    _approx_start,
    _build_krx_name_map,
    _build_name_map,
    _compute_stock_scores,
    _compute_tech_indicators,
    _fetch_fundamentals,
    _fetch_market_cap,
    _fetch_price_change,
    _is_nan,
    _pct_dec,
    _prev_business_day,
    _safe_bool,
    _safe_float,
    _ymd,
)

logger = logging.getLogger(__name__)

RECOMMEND_CACHE_TTL = 6 * 3600  # EOD 스냅샷은 6시간 캐시(metrics 와 동일)

_INDEX = "KOSPI200"


def compute_kospi200_scored(as_of: date) -> RecommendOut:
    """as_of 시점 KOSPI200 구성종목의 팩터별 세부점수·메타데이터를 계산한다(블로킹).

    반환 items 의 score_* 세부점수는 KOSPI200 크로스섹션 내 z-score 이며 가중치와
    무관하다(프론트가 사용자 비중으로 가중합해 Top-N 을 산출). members 조회 실패
    (KRX 미인증 등)나 시세 부재 시 빈 결과를 반환한다.
    """
    members = krx_index.index_members(as_of, _INDEX)
    if not members:
        logger.warning("KOSPI200 구성종목을 확보하지 못함(미인증/무자료) — 빈 추천 반환")
        return RecommendOut(
            as_of=as_of, index=_INDEX, count=0,
            opendart_enabled=opendart.is_enabled(), items=[],
        )
    members = [str(c).zfill(6) for c in members]
    member_set = set(members)

    as_of_ymd = _ymd(as_of)
    prev_day_ymd = _ymd(_prev_business_day(as_of))
    mkts = ["KOSPI"]  # KOSPI200 은 전량 KOSPI 종목

    # ── 일괄 조회(전 종목) 후 멤버로 한정 ──
    fund_df = _fetch_fundamentals(as_of_ymd, mkts)
    cap_df = _fetch_market_cap(as_of_ymd, mkts)
    if cap_df.empty:
        logger.warning("KOSPI 시가총액 데이터 없음(as_of=%s) — 빈 추천 반환", as_of_ymd)
        return RecommendOut(
            as_of=as_of, index=_INDEX, count=0,
            opendart_enabled=opendart.is_enabled(), items=[],
        )

    pc_1d = _fetch_price_change(prev_day_ymd, as_of_ymd, mkts)
    pc_21d = _fetch_price_change(_ymd(_approx_start(as_of, 21)), as_of_ymd, mkts)
    pc_63d = _fetch_price_change(_ymd(_approx_start(as_of, 63)), as_of_ymd, mkts)
    pc_126d = _fetch_price_change(_ymd(_approx_start(as_of, 126)), as_of_ymd, mkts)

    # ── 멤버 한정 병합 프레임 구성 ──
    cap_df = cap_df.copy()
    cap_df.index = cap_df.index.astype(str).str.zfill(6)
    merged = cap_df[cap_df.index.isin(member_set)].copy()

    def _reindex(frame: pd.DataFrame, col: str) -> pd.Series:
        """price_change/fundamental 프레임의 col 을 멤버 인덱스로 정렬한 Series."""
        if frame is None or frame.empty or col not in frame.columns:
            return pd.Series(np.nan, index=merged.index)
        s = frame.copy()
        s.index = s.index.astype(str).str.zfill(6)
        return s[col].reindex(merged.index)

    for col in ("PER", "PBR", "DIV"):
        merged[col] = _reindex(fund_df, col)
    merged["price_close"] = _reindex(pc_1d, "종가")
    merged["change_rate"] = _pct_dec(_reindex(pc_1d, "등락률"))
    merged["mom_1m"] = _pct_dec(_reindex(pc_21d, "등락률"))
    merged["mom_3m"] = _pct_dec(_reindex(pc_63d, "등락률"))
    merged["mom_6m"] = _pct_dec(_reindex(pc_126d, "등락률"))
    tv21 = _reindex(pc_21d, "거래대금")
    merged["avg_value_20"] = tv21 / 20.0

    # 종가 폴백: 시가총액 / 상장주식수
    if "상장주식수" in merged.columns:
        shares = merged["상장주식수"].replace(0, np.nan)
        fallback = (merged["시가총액"] / shares).round(0)
        merged["price_close"] = merged["price_close"].fillna(fallback)

    # ── 기술지표(멤버별 OHLCV) ──
    hist_start_ymd = _ymd(_approx_start(as_of, 270, buffer=30))
    tech_rows: list[dict] = []
    for code in merged.index:
        tech = _compute_tech_indicators(str(code).zfill(6), hist_start_ymd, as_of_ymd)
        tech["code"] = str(code).zfill(6)
        tech_rows.append(tech)
    tech_df = pd.DataFrame(tech_rows).set_index("code") if tech_rows else pd.DataFrame()
    for col in ("high_52w_ratio", "rsi14", "vol_ann", "mdd_252",
                "trend_aligned", "above_sma200"):
        if col in tech_df.columns:
            merged[col] = tech_df[col].reindex(merged.index)

    # ── OpenDART 퀄리티·성장 팩터(있으면) ──
    try:
        qmetrics = opendart.metrics_by_symbol(members, as_of)
    except Exception:  # noqa: BLE001
        logger.warning("추천 OpenDART 퀄리티/성장 팩터 조회 실패 — 중립 처리", exc_info=True)
        qmetrics = {}
    if qmetrics:
        qdf = pd.DataFrame.from_dict(qmetrics, orient="index")
        for col in ("roe", "debt_ratio", "fcf", "f_score",
                    "op_growth", "net_growth", "turnaround"):
            if col in qdf.columns:
                merged[col] = qdf[col].reindex(merged.index)

    # ── 팩터별 세부점수(가중치 무관) ──
    scored = _compute_stock_scores(merged)

    seed_names = _build_name_map()
    krx_names = _build_krx_name_map(pc_1d, pc_21d, pc_63d, pc_126d)

    items: list[RecommendMember] = []
    for code, row in scored.iterrows():
        code_str = str(code).zfill(6)
        items.append(RecommendMember(
            code=code_str,
            name=seed_names.get(code_str) or krx_names.get(code_str) or code_str,
            price=int(round(float(row["price_close"]))) if not _is_nan(row.get("price_close")) else 0,
            change_rate=_safe_float(row.get("change_rate")),
            market_cap=int(row["시가총액"]) if not _is_nan(row.get("시가총액")) else 0,
            avg_value_20=_safe_float(row.get("avg_value_20")) or 0.0,
            per=_safe_float(row.get("PER")),
            pbr=_safe_float(row.get("PBR")),
            div=_safe_float(row.get("DIV")),
            mom_3m=_safe_float(row.get("mom_3m")),
            mom_6m=_safe_float(row.get("mom_6m")),
            vol_ann=_safe_float(row.get("vol_ann")),
            mdd_252=_safe_float(row.get("mdd_252")),
            trend_aligned=_safe_bool(row.get("trend_aligned")),
            above_sma200=_safe_bool(row.get("above_sma200")),
            score_momentum=_safe_float(row.get("score_momentum")),
            score_value=_safe_float(row.get("score_value")),
            score_lowvol=_safe_float(row.get("score_lowvol")),
            score_quality=_safe_float(row.get("score_quality")),
            score_growth=_safe_float(row.get("score_growth")),
        ))

    # 시가총액 내림차순 기본 정렬(프론트가 가중점수로 재정렬).
    items.sort(key=lambda m: m.market_cap, reverse=True)
    logger.info("KOSPI200 추천 스코어링 완료: %d종목(as_of=%s)", len(items), as_of_ymd)
    return RecommendOut(
        as_of=as_of, index=_INDEX, count=len(items),
        opendart_enabled=opendart.is_enabled(), items=items,
    )
