"""종목 지표 계산 — 일괄 조회→필터→기술지표→종합점수의 4단계 파이프라인.

## 성능·캐싱
- 기술지표(RSI/vol/MDD/정배열)는 유동성·시총 필터를 통과한 후보군(≤200종목)에 대해서만
  load_ohlcv 로 계산해 호출량을 제한한다.

## 알려진 한계
- 생존편향: pykrx는 현재 상장 종목 기준으로 과거 데이터를 제공하므로 역사적 상폐 종목은
  포함되지 않는다.
- 52주 고가 비율(high_52w_ratio)은 close 기준 252영업일 최고값으로 계산한다(daily high 아님).
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from app.schemas.metrics import StockMetric, StocksOut
from app.services.backtest.signals import _rsi, _sma  # 백테스트와 동일 정의 재사용
from app.services.data.loader import load_ohlcv
from app.services.metrics.common import (
    _approx_start,
    _compute_mdd,
    _compute_vol_ann,
    _is_nan,
    _mkts,
    _pct_dec,
    _prev_business_day,
    _safe_bool,
    _safe_float,
    _ymd,
)
from app.services.metrics.factors import _compute_stock_scores
from app.services.metrics.fetch import (
    _fetch_fundamentals,
    _fetch_market_cap,
    _fetch_price_change,
)
from app.services.metrics.names import _build_krx_name_map, _build_name_map

logger = logging.getLogger("app.services.metrics")

# Redis 캐시 TTL (6시간 — EOD 스냅샷은 자주 변하지 않음)
STOCKS_CACHE_TTL = 6 * 3600

# 기술지표 계산 후보군 상한
_MAX_CANDIDATES = 200

# 기본 필터 기준 (요청 파라미터로 오버라이드 가능)
DEFAULT_MIN_VALUE = 500_000_000      # 5억 원
DEFAULT_MIN_MCAP = 100_000_000_000   # 1,000억 원


def compute_stocks(market: str, as_of: date) -> StocksOut:
    """종목 지표를 계산해 반환한다.

    Phase 1: pykrx 일괄 조회(펀더멘털·시총·모멘텀).
    Phase 2: 유동성·시총 필터 적용 → 후보군 도출.
    Phase 3: 후보군(≤200종목)에 대해 개별 OHLCV 조회 → 기술지표 계산.
    Phase 4: 크로스섹션 z-score로 종합 점수 산출.

    동기(블로킹) 함수이므로 호출자는 asyncio.to_thread로 실행해야 한다.

    한계:
      - trading_days ≥ 252 필터는 OHLCV 조회 시 실제 봉 수로 확인한다.
      - 관리종목·거래정지 종목은 OHLCV 부재로 자연 제외되는 경우가 대부분이나
        pykrx가 데이터를 포함하면 필터링되지 않을 수 있다.
    """
    as_of_ymd = _ymd(as_of)
    prev_day = _prev_business_day(as_of)
    prev_day_ymd = _ymd(prev_day)
    mkts = _mkts(market)

    # ── 기간별 시작일 계산 ──
    start_21d = _ymd(_approx_start(as_of, 21))    # mom_1m + avg_value_20
    start_63d = _ymd(_approx_start(as_of, 63))    # mom_3m
    start_126d = _ymd(_approx_start(as_of, 126))  # mom_6m
    start_252d = _ymd(_approx_start(as_of, 252))  # 12M (mom_12_1 분모 시작)
    end_21d = _ymd(_approx_start(as_of, 21))      # mom_12_1 분자 종료(~21bday ago)

    logger.info("종목 지표 계산 시작: as_of=%s market=%s", as_of_ymd, market)

    # ── Phase 1: 일괄 데이터 조회 ──
    fund_df = _fetch_fundamentals(as_of_ymd, mkts)
    cap_df = _fetch_market_cap(as_of_ymd, mkts)

    # 1-day price change: 종가(price) + 당일 등락률
    pc_1d = _fetch_price_change(prev_day_ymd, as_of_ymd, mkts)
    # 21-day: mom_1m + 총 거래대금(avg_value_20 계산용)
    pc_21d = _fetch_price_change(start_21d, as_of_ymd, mkts)
    # 63-day: mom_3m
    pc_63d = _fetch_price_change(start_63d, as_of_ymd, mkts)
    # 126-day: mom_6m
    pc_126d = _fetch_price_change(start_126d, as_of_ymd, mkts)
    # 12-1 모멘텀: start_252d ~ end_21d
    pc_12_1 = _fetch_price_change(start_252d, end_21d, mkts)

    if cap_df.empty:
        logger.warning("시가총액 데이터 없음: as_of=%s market=%s", as_of_ymd, market)
        return StocksOut(as_of=as_of, count=0, items=[])

    # ── 데이터 병합 ──
    merged = cap_df.copy()

    # 시장 태그는 cap_df 가 이미 싣는다 — 로컬 저장소 도입(§49 B1) 이후
    # _fetch_market_cap 도 시장별로 market 을 태깅한다. 예전에는 fund_df 에서만 왔기에
    # join 할 때 market 을 함께 끌어왔는데, 지금 그대로 두면 양쪽에 같은 컬럼이 있어
    # pandas join 이 "columns overlap but no suffix specified" 로 죽는다. 여기서는
    # 펀더멘털 3종만 붙이고, 시장 태그는 cap_df 것을 쓴다.
    fund_cols = [c for c in ("PER", "PBR", "DIV") if c in fund_df.columns]
    if not fund_df.empty and fund_cols:
        merged = merged.join(fund_df[fund_cols], how="left")
    for col in ("PER", "PBR", "DIV"):
        if col not in merged.columns:
            merged[col] = np.nan

    # 시장 태그가 없거나(구 스키마 잔재) 비어 있으면 요청 시장으로 채운다.
    fallback_market = market if market != "ALL" else "KOSPI"
    if "market" not in merged.columns:
        merged["market"] = fallback_market
    else:
        merged["market"] = merged["market"].fillna(fallback_market)

    # 1-day 종가 및 등락률
    if not pc_1d.empty:
        if "종가" in pc_1d.columns:
            merged["price_close"] = pc_1d["종가"]
        if "등락률" in pc_1d.columns:
            merged["change_rate"] = _pct_dec(pc_1d["등락률"])

    # 21-day 거래대금(일평균 ≈ 20 영업일)
    if not pc_21d.empty and "거래대금" in pc_21d.columns:
        merged["avg_value_20"] = pc_21d["거래대금"] / 20.0

    # 기간별 모멘텀(등락률 → 소수 수익률)
    for src, dst in ((pc_21d, "mom_1m"), (pc_63d, "mom_3m"),
                     (pc_126d, "mom_6m"), (pc_12_1, "mom_12_1")):
        if not src.empty and "등락률" in src.columns:
            merged[dst] = _pct_dec(src["등락률"])

    # price 계산: 종가 우선, 없으면 시가총액/상장주식수
    if "price_close" not in merged.columns:
        if "시가총액" in merged.columns and "상장주식수" in merged.columns:
            shares = merged["상장주식수"].replace(0, np.nan)
            merged["price_close"] = (merged["시가총액"] / shares).round(0)
        else:
            merged["price_close"] = np.nan

    # ── Phase 2: 유동성·시총 필터 ──
    # 기본 필터: avg_value_20 ≥ 5억, 시가총액 ≥ 1,000억
    has_mcap = "시가총액" in merged.columns
    has_avg_val = "avg_value_20" in merged.columns

    mask = pd.Series(True, index=merged.index)
    if has_mcap:
        mask &= merged["시가총액"].fillna(0) >= DEFAULT_MIN_MCAP
    if has_avg_val:
        mask &= merged["avg_value_20"].fillna(0) >= DEFAULT_MIN_VALUE
    # mom_6m 결측 종목은 스코어링 불가 → 필터링 대상 (Phase 4에서 score=NaN으로 처리)

    candidates = merged[mask].copy()
    logger.info("후보 종목 수(필터 적용 후): %d", len(candidates))

    # 최대 200종목으로 제한 (시가총액 상위 200종목 선택)
    if len(candidates) > _MAX_CANDIDATES:
        if "시가총액" in candidates.columns:
            candidates = candidates.nlargest(_MAX_CANDIDATES, "시가총액")
        else:
            candidates = candidates.head(_MAX_CANDIDATES)

    # ── Phase 3: 기술지표 계산 (개별 OHLCV 조회) ──
    # 종목 이름: 큐레이트된 내장 카탈로그(정확한 표기, 예: "KT&G")를 1순위로 하고,
    # 없으면 get_market_price_change 의 '종목명'(이미 받은 데이터, 추가 호출 없음)을
    # 폴백으로 쓴다. 카탈로그가 FDR 상장목록을 병합하지 못하는 환경에서도 전 종목
    # 이름이 채워진다.
    krx_name_map = _build_krx_name_map(pc_1d, pc_21d, pc_63d, pc_126d)
    seed_name_map = _build_name_map()
    hist_start_ohlcv = _ymd(_approx_start(as_of, 270, buffer=30))  # 270bday + 여유

    tech_rows: list[dict] = []
    for code in candidates.index:
        code_str = str(code).zfill(6)
        tech = _compute_tech_indicators(code_str, hist_start_ohlcv, as_of_ymd)
        tech["code"] = code_str
        tech_rows.append(tech)

    tech_df = pd.DataFrame(tech_rows).set_index("code") if tech_rows else pd.DataFrame()

    # 기술지표를 candidates에 병합
    if not tech_df.empty:
        candidates.index = candidates.index.astype(str).str.zfill(6)
        for col in ["high_52w_ratio", "rsi14", "vol_ann", "mdd_252",
                    "trend_aligned", "above_sma200", "valid_bdays"]:
            if col in tech_df.columns:
                candidates[col] = tech_df[col]

    # trading_days ≥ 252 필터 (실제 OHLCV 봉 수 기반)
    if "valid_bdays" in candidates.columns:
        candidates = candidates[candidates["valid_bdays"].fillna(0) >= 252]

    # ── Phase 4: 종합 점수 계산 ──
    candidates = _compute_stock_scores(candidates)

    # ── 결과 조립 ──
    items: list[StockMetric] = []
    for code, row in candidates.iterrows():
        code_str = str(code).zfill(6)
        mkt_label = str(row.get("market", "")) if not _is_nan(row.get("market")) else market

        items.append(StockMetric(
            code=code_str,
            name=seed_name_map.get(code_str) or krx_name_map.get(code_str) or code_str,
            market=mkt_label,
            price=int(round(float(row["price_close"]))) if not _is_nan(row.get("price_close")) else 0,
            change_rate=_safe_float(row.get("change_rate")),
            market_cap=int(row["시가총액"]) if not _is_nan(row.get("시가총액")) else 0,
            avg_value_20=float(row["avg_value_20"]) if not _is_nan(row.get("avg_value_20")) else 0.0,
            per=_safe_float(row.get("PER")),
            pbr=_safe_float(row.get("PBR")),
            div=_safe_float(row.get("DIV")),
            mom_1m=_safe_float(row.get("mom_1m")),
            mom_3m=_safe_float(row.get("mom_3m")),
            mom_6m=_safe_float(row.get("mom_6m")),
            mom_12_1=_safe_float(row.get("mom_12_1")),
            high_52w_ratio=_safe_float(row.get("high_52w_ratio")),
            rsi14=_safe_float(row.get("rsi14")),
            vol_ann=_safe_float(row.get("vol_ann")),
            mdd_252=_safe_float(row.get("mdd_252")),
            trend_aligned=_safe_bool(row.get("trend_aligned")),
            above_sma200=_safe_bool(row.get("above_sma200")),
            score=_safe_float(row.get("score")),
            score_value=_safe_float(row.get("score_value")),
            score_momentum=_safe_float(row.get("score_momentum")),
            score_lowvol=_safe_float(row.get("score_lowvol")),
        ))

    logger.info("종목 지표 계산 완료: %d건", len(items))
    return StocksOut(as_of=as_of, count=len(items), items=items)


def _compute_tech_indicators(code: str, start_ymd: str, end_ymd: str) -> dict:
    """종목 1개의 기술지표를 OHLCV에서 계산한다.

    signals.py 헬퍼(_sma, _rsi)를 재사용해 백테스트 정의와 일치시킨다.

    반환 딕셔너리:
      high_52w_ratio, rsi14, vol_ann, mdd_252, trend_aligned, above_sma200, valid_bdays
    """
    result: dict = {
        "high_52w_ratio": None,
        "rsi14": None,
        "vol_ann": None,
        "mdd_252": None,
        "trend_aligned": None,
        "above_sma200": None,
        "valid_bdays": 0,
    }
    try:
        df = load_ohlcv(code, start_ymd, end_ymd)
        if df is None or df.empty or "close" not in df.columns:
            return result

        close = df["close"].astype(float)
        result["valid_bdays"] = len(close)

        if len(close) < 20:
            return result

        # 52주 고가 대비 (close 기준 252봉 최고값)
        if len(close) >= 252:
            hi_52w = close.tail(252).max()
            if hi_52w > 0:
                result["high_52w_ratio"] = _safe_float(close.iloc[-1] / hi_52w)

        # RSI14 (signals.py의 Wilder RSI 재사용)
        if len(close) >= 28:  # RSI 최소 2×period
            rsi_series = _rsi(close, 14)
            last_rsi = rsi_series.iloc[-1]
            result["rsi14"] = _safe_float(last_rsi)

        # 연율 변동성
        result["vol_ann"] = _compute_vol_ann(close.tail(253))

        # MDD_252 (여기선 len(close) >= 20 이 보장됨)
        result["mdd_252"] = _compute_mdd(close.tail(252))

        # 정배열: SMA5 > SMA20 > SMA60 > SMA120 (signals.py의 _sma 재사용)
        if len(close) >= 120:
            sma5 = _sma(close, 5).iloc[-1]
            sma20 = _sma(close, 20).iloc[-1]
            sma60 = _sma(close, 60).iloc[-1]
            sma120 = _sma(close, 120).iloc[-1]
            if all(np.isfinite([sma5, sma20, sma60, sma120])):
                result["trend_aligned"] = bool(sma5 > sma20 > sma60 > sma120)

        # 종가 > SMA200
        if len(close) >= 200:
            sma200 = _sma(close, 200).iloc[-1]
            if np.isfinite(sma200):
                result["above_sma200"] = bool(close.iloc[-1] > sma200)

    except Exception:
        logger.debug("기술지표 계산 실패 (%s)", code, exc_info=True)

    return result
