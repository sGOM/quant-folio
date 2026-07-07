"""지표 화면 서비스 — 섹터·종목 지표 일괄 계산 및 캐시 관리.

## 데이터 소스
- pykrx 일괄 조회를 적극 활용해 API 호출 비용을 최소화한다.
  - get_market_fundamental  : 전 종목 PER/PBR/DIV (한 번에)
  - get_market_cap           : 전 종목 시가총액/거래대금 (한 번에)
  - get_market_price_change  : 기간 등락률·거래대금 (모멘텀 일괄)
  - get_index_ticker_list / get_index_ohlcv : 업종지수
  - get_index_portfolio_deposit_file       : 업종 구성종목

## 성능·캐싱
- 모든 pykrx/FDR 호출은 블로킹이므로 호출자(라우트)가 asyncio.to_thread 로 실행.
- 결과는 as_of + market 키로 Redis 에 캐시 (TTL ≈ 6시간).
- 기술지표(RSI/vol/MDD/정배열)는 유동성·시총 필터를 통과한 후보군(≤200종목)에 대해서만
  load_ohlcv 로 계산해 호출량을 제한한다.

## 알려진 한계
- 관리종목·거래정지 종목 제외: pykrx 가 해당 종목 데이터를 포함하는 경우 best-effort 로
  처리한다(OHLCV 부재 시 자연히 필터링).
- 생존편향: pykrx는 현재 상장 종목 기준으로 과거 데이터를 제공하므로 역사적 상폐 종목은
  포함되지 않는다.
- 미래참조 방지: as_of는 직전 확정 영업일(당일 종가 미확정 방지)로 고정한다.
- get_market_price_change 의 등락률은 '시작 가격→종료 종가' 기준이며, 정확한 close-to-close
  모멘텀과 미미한 차이가 있을 수 있다.
- 업종지수 RS_126은 동일 시장(KOSPI/KOSDAQ) 기준으로 계산한다; KRX 혼합 업종은
  소속 시장을 기준으로 분류한다.
- 52주 고가 비율(high_52w_ratio)은 close 기준 252영업일 최고값으로 계산한다(daily high 아님).
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.schemas.metrics import SectorMetric, SectorsOut, StockMetric, StocksOut
from app.services.backtest.signals import _rsi, _sma  # 백테스트와 동일 정의 재사용
from app.services.data.loader import load_ohlcv
from app.services.market import is_business_day
from app.services.symbols import get_catalog

logger = logging.getLogger(__name__)

# Redis 캐시 TTL (6시간 — EOD 스냅샷은 자주 변하지 않음)
SECTORS_CACHE_TTL = 6 * 3600
STOCKS_CACHE_TTL = 6 * 3600

# 기준 업종지수: RS_126 분모 (pykrx 업종지수 코드)
_KOSPI_REF_TICKER = "1001"   # KOSPI 종합지수
_KOSDAQ_REF_TICKER = "2001"  # KOSDAQ 종합지수

# 기술지표 계산 후보군 상한
_MAX_CANDIDATES = 200

# 기본 필터 기준 (요청 파라미터로 오버라이드 가능)
DEFAULT_MIN_VALUE = 500_000_000      # 5억 원
DEFAULT_MIN_MCAP = 100_000_000_000   # 1,000억 원


# ─────────────────────── 공통 헬퍼 ───────────────────────


def _last_business_day() -> date:
    """직전 확정 영업일을 반환한다(당일 미확정 데이터 혼입 방지).

    오늘 날짜에서 하루씩 거슬러 올라가며 is_business_day를 확인한다.
    """
    d = date.today() - timedelta(days=1)
    for _ in range(14):  # 최대 2주 탐색 (연휴 대비)
        if is_business_day(d):
            return d
        d -= timedelta(days=1)
    # 그래도 못 찾으면 최근 월요일 반환(극단적 폴백)
    return date.today() - timedelta(days=date.today().weekday() + 7)


def _prev_business_day(d: date) -> date:
    """d 바로 이전 영업일을 반환한다."""
    candidate = d - timedelta(days=1)
    for _ in range(10):
        if is_business_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return d - timedelta(days=3)


def _approx_start(as_of: date, bdays: int, buffer: int = 14) -> date:
    """as_of 기준 bdays 영업일 이전의 시작일을 캘린더 일수로 근사한다.

    7/5 변환 + 여유 버퍼를 더해 주말·공휴일로 인한 부족분을 방지한다.
    """
    cal_days = int(bdays * 7 / 5) + buffer
    return as_of - timedelta(days=cal_days)


def _ymd(d: date) -> str:
    """날짜를 pykrx 형식 문자열(YYYYMMDD)로 변환."""
    return d.strftime("%Y%m%d")


def _pct_dec(series: pd.Series) -> pd.Series:
    """pykrx 퍼센트 값을 소수로 변환 (1.23% → 0.0123)."""
    return series / 100.0


def _safe_float(val: Any) -> float | None:
    """NaN/None/무한대를 None으로 변환해 JSON-safe float를 반환."""
    try:
        v = float(val)
        if not np.isfinite(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _compute_mdd(close: pd.Series) -> float | None:
    """Rolling 고점 대비 최대낙폭(MDD)을 계산한다. 결과는 음수 소수."""
    if close.empty or len(close) < 2:
        return None
    peak = close.expanding().max()
    dd = (close - peak) / peak
    return _safe_float(dd.min())


def _compute_vol_ann(close: pd.Series) -> float | None:
    """일간 로그수익률의 연율 변동성(std × √252)을 계산한다."""
    if len(close) < 20:
        return None
    log_ret = np.log(close / close.shift(1)).dropna()
    if len(log_ret) < 10:
        return None
    return _safe_float(log_ret.std() * np.sqrt(252))


def _mkts(market: str) -> list[str]:
    """API market 파라미터를 pykrx market 문자열 목록으로 변환한다."""
    if market.upper() == "ALL":
        return ["KOSPI", "KOSDAQ"]
    return [market.upper()]


# ─────────────────────── pykrx 일괄 조회 헬퍼 ───────────────────────

# 펀더멘털(PER/PBR/DIV) 프로세스 내 LRU 캐시.
# 특정 as_of 일자의 전 종목 펀더멘털은 확정 후 변하지 않으므로 (as_of, 시장) 단위로
# 캐시하면 동일/유사 기간을 반복 백테스트할 때 pykrx 왕복을 크게 줄인다
# (예: score 방식 리밸런싱은 리밸런싱일마다 as_of 스냅샷을 조회한다). 워커 프로세스
# 수명 동안 유지되며, 실패(빈 결과)는 캐시하지 않아 다음 호출 때 재시도된다.
_FUND_CACHE: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
_FUND_CACHE_MAX = 64  # 스냅샷 상한(초과 시 가장 오래된 항목 축출)


def _fetch_fundamentals(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 펀더멘털(PER/PBR/DIV)을 시장별로 조회해 합산한다.

    컬럼: PER, PBR, DIV + market(추가). 티커 인덱스.
    PER=0은 적자(undefined)로 간주해 음수로 취급한다.
    (as_of, 시장) 키로 프로세스 내 LRU 캐시하며, 호출자가 결과를 변형해도 캐시가
    오염되지 않도록 항상 사본을 반환한다.
    """
    key = (as_of_ymd, tuple(sorted(mkts)))
    cached = _FUND_CACHE.get(key)
    if cached is not None:
        _FUND_CACHE.move_to_end(key)  # 최근 사용으로 갱신
        return cached.copy()

    from pykrx import stock  # 블로킹 임포트 지연(스레드 호출 컨텍스트)

    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = stock.get_market_fundamental(as_of_ymd, market=mkt)
            if df is None or df.empty:
                continue
            df = df[["PER", "PBR", "DIV"]].copy()
            df["market"] = mkt
            # PER=0 → NaN 처리 (pykrx 는 적자 종목에 0 반환)
            df.loc[df["PER"] <= 0, "PER"] = np.nan
            frames.append(df)
        except Exception:
            logger.warning("펀더멘털 조회 실패 (%s %s)", mkt, as_of_ymd, exc_info=True)

    if not frames:
        # 빈 결과는 캐시하지 않는다(일시적 조회 실패를 영구화하지 않기 위함).
        return pd.DataFrame(columns=["PER", "PBR", "DIV", "market"])

    result = pd.concat(frames)
    _FUND_CACHE[key] = result.copy()
    _FUND_CACHE.move_to_end(key)
    if len(_FUND_CACHE) > _FUND_CACHE_MAX:
        _FUND_CACHE.popitem(last=False)  # 가장 오래된 항목 축출
    return result


def _fetch_market_cap(as_of_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """전 종목 시가총액·상장주식수·거래대금을 조회한다.

    컬럼: 시가총액, 상장주식수, 거래대금. 티커 인덱스.
    """
    from pykrx import stock

    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = stock.get_market_cap(as_of_ymd, market=mkt)
            if df is None or df.empty:
                continue
            # 필요 컬럼만 선택 (버전 차이 대비)
            cols = [c for c in ["시가총액", "거래량", "거래대금", "상장주식수"] if c in df.columns]
            frames.append(df[cols])
        except Exception:
            logger.warning("시가총액 조회 실패 (%s %s)", mkt, as_of_ymd, exc_info=True)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def _fetch_price_change(start_ymd: str, end_ymd: str, mkts: list[str]) -> pd.DataFrame:
    """기간 등락률·거래대금을 전 종목 일괄 조회한다.

    컬럼: 시가, 종가, 변동폭, 등락률, 거래량, 거래대금. 티커 인덱스.
    등락률은 pykrx 원값 그대로(%) — 호출자가 /100으로 변환한다.
    """
    from pykrx import stock

    frames: list[pd.DataFrame] = []
    for mkt in mkts:
        try:
            df = stock.get_market_price_change(start_ymd, end_ymd, market=mkt)
            if df is None or df.empty:
                continue
            frames.append(df)
        except Exception:
            logger.warning("가격변동 조회 실패 (%s~%s %s)", start_ymd, end_ymd, mkt, exc_info=True)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def _fetch_index_ohlcv(start_ymd: str, end_ymd: str, ticker: str) -> pd.DataFrame | None:
    """업종지수 OHLCV를 조회한다. 실패 시 None 반환.

    pykrx 한글 컬럼 → 영문 변환:
      시가→open, 고가→high, 저가→low, 종가→close,
      거래량→volume, 거래대금→trading_value
    """
    from pykrx import stock

    try:
        df = stock.get_index_ohlcv(start_ymd, end_ymd, ticker)
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low", "종가": "close",
            "거래량": "volume", "거래대금": "trading_value",
        })
        return df
    except Exception:
        logger.debug("업종지수 OHLCV 조회 실패 (%s)", ticker, exc_info=True)
        return None


def _fetch_index_tickers(date_ymd: str, mkt: str) -> list[str]:
    """업종지수 코드 목록을 반환한다."""
    from pykrx import stock

    try:
        result = stock.get_index_ticker_list(date=date_ymd, market=mkt)
        if result is None:
            return []
        return list(result)
    except Exception:
        logger.warning("업종지수 목록 조회 실패 (%s %s)", mkt, date_ymd, exc_info=True)
        return []


def _get_index_name(date_ymd: str, ticker: str) -> str:
    """업종지수 이름을 반환한다. 실패 시 ticker 코드를 반환."""
    from pykrx import stock

    try:
        # pykrx 버전에 따라 인자 형식이 다를 수 있으므로 두 가지 시도
        try:
            name = stock.get_index_ticker_name(ticker)
        except TypeError:
            name = stock.get_index_ticker_name(date_ymd, ticker)
        return str(name).strip() if name else ticker
    except Exception:
        return ticker


# ─────────────────────── 종합 점수 계산 ───────────────────────


def _winsorize_zscore(s: pd.Series, invert: bool = False) -> pd.Series:
    """1/99% 윈저라이징 후 z-score를 계산한다.

    :param s: 입력 시계열 (NaN 포함 허용)
    :param invert: True이면 부호 반전 (낮을수록 좋은 지표 처리)
    :return: z-score Series (입력과 동일 인덱스, NaN 유지)
    """
    valid = s.dropna()
    if len(valid) < 3:
        return pd.Series(np.nan, index=s.index)

    lo = valid.quantile(0.01)
    hi = valid.quantile(0.99)
    clipped = s.clip(lower=lo, upper=hi)
    mu = clipped.mean()
    std = clipped.std(ddof=0)

    if std == 0 or not np.isfinite(std):
        return pd.Series(0.0, index=s.index)

    z = (clipped - mu) / std
    return -z if invert else z


#: 기본 팩터 카테고리 가중치(모멘텀/밸류/저변동/퀄리티/성장). 합=1.0.
#: quality·growth 기본 0.0 — OpenDART 재무데이터가 필요하며, 배선하지 않은 기존
#: 전략은 해당 컬럼 부재로 중립(0) 처리되어 영향받지 않는다.
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    "momentum": 0.4, "value": 0.3, "lowvol": 0.3, "quality": 0.0, "growth": 0.0,
}


def _compute_stock_scores(
    df: pd.DataFrame, weights: dict[str, float] | None = None
) -> pd.DataFrame:
    """후보 종목 데이터프레임에 종합 점수(score, score_*) 컬럼을 추가한다.

    방향 정규화:
      - PER, PBR, vol_ann: 낮을수록 좋음 → invert=True
      - mdd_252: 음수값, 0에 가까울수록(작은 낙폭) 좋음 → 높을수록 좋으므로 invert 불필요
      - DIV, 모멘텀 지표, high_52w_ratio: 높을수록 좋음 → invert=False

    결측 처리:
      - PER 음수(적자) 종목: 밸류 z에서 PER 제외, 중립(0) 처리
      - 모멘텀 결측 종목(mom_6m=NaN): score=NaN → 랭킹 제외
      - 기타 결측: 중립(0) 처리

    :param weights: 카테고리 합성 가중치 {"momentum","value","lowvol"}(합=1.0 권장).
        None 이면 DEFAULT_FACTOR_WEIGHTS(0.4/0.3/0.3) 사용. 리밸런싱 전략의
        RebalanceSelection.factor_weights 로 전략별 커스터마이즈가 가능하다
        (스키마 레벨 합=1 검증은 app.schemas.strategy.FactorWeights 가 수행하므로
        여기서는 재검증하지 않는다).
    """
    w = weights or DEFAULT_FACTOR_WEIGHTS
    result = df.copy()

    # ── 모멘텀 카테고리 z-score ──
    z_m1 = _winsorize_zscore(df.get("mom_1m", pd.Series(np.nan, index=df.index)))
    z_m3 = _winsorize_zscore(df.get("mom_3m", pd.Series(np.nan, index=df.index)))
    z_m6 = _winsorize_zscore(df.get("mom_6m", pd.Series(np.nan, index=df.index)))
    z_h52 = _winsorize_zscore(df.get("high_52w_ratio", pd.Series(np.nan, index=df.index)))

    # 각 z-score 결측은 0(중립)으로 채운 뒤 평균 → 전체 결측이면 NaN
    mom_stack = pd.concat([
        z_m1.fillna(0), z_m3.fillna(0), z_m6.fillna(0), z_h52.fillna(0)
    ], axis=1)
    score_momentum = mom_stack.mean(axis=1)
    # mom_6m 결측 종목은 모멘텀 점수 NaN → 랭킹 제외
    score_momentum = score_momentum.where(df.get("mom_6m", pd.Series(np.nan, index=df.index)).notna())

    # ── 밸류에이션 카테고리 z-score ──
    # 컬럼명은 대문자 PER/PBR/DIV 로 통일한다. 데이터 생성부(_fetch_fundamentals,
    # compute_stocks, compute_universe_scores)가 모두 대문자로 채우므로 여기서도
    # 대문자로 읽어야 밸류 팩터가 실제로 반영된다(소문자로 읽으면 항상 결측→중립 0
    # 이 되어 밸류 가중치가 통째로 무시되는 버그가 있었다).
    per_col = df.get("PER", pd.Series(np.nan, index=df.index))
    per_pos = per_col.where(per_col > 0)   # 음수 PER → NaN
    z_per = _winsorize_zscore(per_pos, invert=True).fillna(0.0)   # 결측→중립
    z_pbr = _winsorize_zscore(
        df.get("PBR", pd.Series(np.nan, index=df.index)), invert=True
    ).fillna(0.0)
    z_div = _winsorize_zscore(
        df.get("DIV", pd.Series(np.nan, index=df.index))
    ).fillna(0.0)
    score_value = pd.concat([z_per, z_pbr, z_div], axis=1).mean(axis=1)

    # ── 저변동 카테고리 z-score ──
    z_vol = _winsorize_zscore(
        df.get("vol_ann", pd.Series(np.nan, index=df.index)), invert=True
    ).fillna(0.0)
    # mdd_252는 음수(예: -0.15), 0에 가까울수록 좋음 → z-score 그대로(반전 불필요)
    z_mdd = _winsorize_zscore(
        df.get("mdd_252", pd.Series(np.nan, index=df.index))
    ).fillna(0.0)
    score_lowvol = pd.concat([z_vol, z_mdd], axis=1).mean(axis=1)

    # ── 퀄리티 카테고리 z-score (OpenDART 재무데이터) ──
    # ROE 높을수록·부채비율 낮을수록·FCF 흑자일수록 우량. FCF 는 원화 절대액이라
    # 크기 편향이 있어 '흑자 여부(1/0)'로 스케일-프리하게 반영한다. 세 컬럼이 모두
    # 없으면(=OpenDART 미배선) score_quality 는 전부 0(중립)이 되어 기존 전략 무영향.
    roe_col = df.get("roe", pd.Series(np.nan, index=df.index))
    debt_col = df.get("debt_ratio", pd.Series(np.nan, index=df.index))
    fcf_col = df.get("fcf", pd.Series(np.nan, index=df.index))
    fscore_col = df.get("f_score", pd.Series(np.nan, index=df.index))
    z_roe = _winsorize_zscore(roe_col).fillna(0.0)
    z_debt = _winsorize_zscore(debt_col, invert=True).fillna(0.0)  # 저부채 우량
    fcf_pos = fcf_col.where(fcf_col.notna())  # NaN 유지
    fcf_pos = (fcf_pos > 0).astype(float).where(fcf_col.notna())   # 흑자=1, 적자=0, 결측 NaN
    z_fcf = _winsorize_zscore(fcf_pos).fillna(0.0)
    z_fscore = _winsorize_zscore(fscore_col).fillna(0.0)  # Piotroski F-Score(0~8), 높을수록 우량
    score_quality = pd.concat([z_roe, z_debt, z_fcf, z_fscore], axis=1).mean(axis=1)

    # ── 성장(실적상향) 카테고리 z-score (OpenDART 재무데이터) ──
    # 영업이익·순이익 YoY 성장률(높을수록 우량) + 흑자전환(1/0). 세 컬럼이 모두
    # 없으면(=OpenDART 미배선) score_growth 는 전부 0(중립) → 기존 전략 무영향.
    z_opg = _winsorize_zscore(
        df.get("op_growth", pd.Series(np.nan, index=df.index))
    ).fillna(0.0)
    z_netg = _winsorize_zscore(
        df.get("net_growth", pd.Series(np.nan, index=df.index))
    ).fillna(0.0)
    z_turn = _winsorize_zscore(
        df.get("turnaround", pd.Series(np.nan, index=df.index))
    ).fillna(0.0)
    score_growth = pd.concat([z_opg, z_netg, z_turn], axis=1).mean(axis=1)

    # ── 종합 점수 ──
    score = (
        w.get("momentum", DEFAULT_FACTOR_WEIGHTS["momentum"]) * score_momentum
        + w.get("value", DEFAULT_FACTOR_WEIGHTS["value"]) * score_value
        + w.get("lowvol", DEFAULT_FACTOR_WEIGHTS["lowvol"]) * score_lowvol
        + w.get("quality", DEFAULT_FACTOR_WEIGHTS["quality"]) * score_quality
        + w.get("growth", DEFAULT_FACTOR_WEIGHTS["growth"]) * score_growth
    )

    result["score_momentum"] = score_momentum.round(4)
    result["score_value"] = score_value.round(4)
    result["score_lowvol"] = score_lowvol.round(4)
    result["score_quality"] = score_quality.round(4)
    result["score_growth"] = score_growth.round(4)
    result["score"] = score.round(4)

    return result


# ─────────────────────── 리밸런싱 유니버스 종합점수 ───────────────────────


def compute_universe_scores(
    symbols: list[str],
    as_of: date,
    factor_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """리밸런싱 selection.method="score" 전용: 지정 종목들의 종합점수를 계산한다.

    compute_stocks() 는 전 시장을 스캔해 시가총액 상위 200종목으로 제한하지만,
    리밸런싱 universe 는 이미 사용자가 지정한 소수 종목(≤50)이므로 해당 종목만
    직접 조회해 점수를 산출한다(불필요한 전 시장 스캔 비용을 피함).

    미래참조 방지: as_of 는 반드시 직전 확정 영업일 이하여야 한다(호출자가
    _last_business_day() 등으로 결정). 장중(당일 미확정 종가) 호출을 막기 위해
    이 함수는 as_of 를 스스로 "오늘"로 보정하지 않는다 — 호출자 책임이다.

    :param symbols: 종목코드 목록(6자리, 미확정 zero-fill 은 함수 내부에서 처리)
    :param as_of: 점수 산출 기준일(확정 영업일)
    :param factor_weights: {"momentum","value","lowvol"} 카테고리 가중치. None 이면 기본값.
    :return: {종목코드: score} dict. 데이터 부족(예: mom_6m 결측)으로 계산 불가한
        종목은 결과에서 제외된다(compute_target_weights 가 자연히 후순위/미선정 처리).
    """
    if not symbols:
        return {}

    codes = [str(s).strip().zfill(6) for s in symbols]
    as_of_ymd = _ymd(as_of)
    mkts = ["KOSPI", "KOSDAQ"]  # universe 종목의 소속 시장을 미리 알 수 없어 양쪽 조회

    fund_df = _fetch_fundamentals(as_of_ymd, mkts)
    pc_21d = _fetch_price_change(_ymd(_approx_start(as_of, 21)), as_of_ymd, mkts)
    pc_63d = _fetch_price_change(_ymd(_approx_start(as_of, 63)), as_of_ymd, mkts)
    pc_126d = _fetch_price_change(_ymd(_approx_start(as_of, 126)), as_of_ymd, mkts)

    rows: list[dict] = []
    for code in codes:
        row: dict = {"code": code}
        if not fund_df.empty and code in fund_df.index:
            f = fund_df.loc[code]
            row["PER"] = f.get("PER")
            row["PBR"] = f.get("PBR")
            row["DIV"] = f.get("DIV")
        if not pc_21d.empty and code in pc_21d.index and "등락률" in pc_21d.columns:
            row["mom_1m"] = float(pc_21d.loc[code, "등락률"]) / 100.0
        if not pc_63d.empty and code in pc_63d.index and "등락률" in pc_63d.columns:
            row["mom_3m"] = float(pc_63d.loc[code, "등락률"]) / 100.0
        if not pc_126d.empty and code in pc_126d.index and "등락률" in pc_126d.columns:
            row["mom_6m"] = float(pc_126d.loc[code, "등락률"]) / 100.0
        rows.append(row)

    df = pd.DataFrame(rows).set_index("code")

    hist_start_ymd = _ymd(_approx_start(as_of, 270, buffer=30))
    tech_rows: list[dict] = []
    for code in codes:
        tech = _compute_tech_indicators(code, hist_start_ymd, as_of_ymd)
        tech["code"] = code
        tech_rows.append(tech)
    tech_df = pd.DataFrame(tech_rows).set_index("code") if tech_rows else pd.DataFrame()

    for col in ["high_52w_ratio", "vol_ann", "mdd_252"]:
        if col in tech_df.columns:
            df[col] = tech_df[col]

    # 퀄리티 팩터(OpenDART 재무데이터, PIT 공시지연). 키 부재 시 {} → 컬럼 미추가로
    # 중립 처리된다. factor_weights.quality>0 인 전략에서만 실제로 점수에 반영된다.
    try:
        from app.services.data import opendart

        qmetrics = opendart.metrics_by_symbol(codes, as_of)
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenDART 퀄리티 팩터 조회 실패 — 중립 처리: %s", e)
        qmetrics = {}
    if qmetrics:
        qdf = pd.DataFrame.from_dict(qmetrics, orient="index")
        for col in ("roe", "debt_ratio", "fcf", "f_score",
                    "op_growth", "net_growth", "turnaround"):
            if col in qdf.columns:
                df[col] = qdf[col].reindex(df.index)

    scored = _compute_stock_scores(df, weights=factor_weights)
    return {
        code: float(val)
        for code, val in scored["score"].items()
        if not _is_nan(val)
    }


# ─────────────────────── 섹터 지표 계산 ───────────────────────


def compute_sectors(market: str, as_of: date) -> SectorsOut:
    """섹터(업종지수) 지표를 계산해 반환한다.

    pykrx KRX 업종지수를 이용해 모멘텀·변동성·RS·밸류(구성종목 중앙값)를 산출한다.
    동기(블로킹) 함수이므로 호출자는 asyncio.to_thread로 실행해야 한다.
    """
    from pykrx import stock as _stock  # noqa: F401 — 임포트 확인용

    as_of_ymd = _ymd(as_of)
    mkts_to_fetch = _mkts(market)

    # 필요한 과거 기간: 252영업일 + 여유 → 약 400 캘린더일
    hist_start = _approx_start(as_of, 252, buffer=30)
    hist_start_ymd = _ymd(hist_start)

    # ── 기준 시장지수 6M 수익률 (RS_126 분모) ──
    ref_returns: dict[str, float | None] = {}
    for ref_mkt, ref_ticker in [("KOSPI", _KOSPI_REF_TICKER), ("KOSDAQ", _KOSDAQ_REF_TICKER)]:
        df_ref = _fetch_index_ohlcv(hist_start_ymd, as_of_ymd, ref_ticker)
        if df_ref is not None and "close" in df_ref.columns and len(df_ref) >= 126:
            c = df_ref["close"]
            ref_ret = _safe_float((c.iloc[-1] / c.iloc[-126] - 1))
        else:
            ref_ret = None
        ref_returns[ref_mkt] = ref_ret

    # ── 업종지수별 지표 계산 ──
    items: list[SectorMetric] = []

    for mkt in mkts_to_fetch:
        tickers = _fetch_index_tickers(as_of_ymd, mkt)
        ref_mkt = mkt  # KOSPI 업종 → KOSPI 기준, KOSDAQ 업종 → KOSDAQ 기준

        for ticker in tickers:
            # 기준 시장지수 자체는 섹터 목록에서 제외
            if ticker in (_KOSPI_REF_TICKER, _KOSDAQ_REF_TICKER):
                continue

            try:
                metric = _compute_one_sector(
                    ticker=ticker,
                    mkt=mkt,
                    date_ymd=as_of_ymd,
                    hist_start_ymd=hist_start_ymd,
                    ref_return=ref_returns.get(ref_mkt),
                )
                if metric is not None:
                    items.append(metric)
            except Exception:
                logger.warning("섹터 계산 오류 (ticker=%s %s)", ticker, mkt, exc_info=True)

    return SectorsOut(as_of=as_of, items=items)


def _compute_one_sector(
    ticker: str,
    mkt: str,
    date_ymd: str,
    hist_start_ymd: str,
    ref_return: float | None,
) -> SectorMetric | None:
    """업종지수 1개의 지표를 계산한다."""
    df = _fetch_index_ohlcv(hist_start_ymd, date_ymd, ticker)
    if df is None or "close" not in df.columns or len(df) < 21:
        return None

    name = _get_index_name(date_ymd, ticker)
    c = df["close"]

    def _mom(n: int) -> float | None:
        if len(c) < n:
            return None
        return _safe_float(c.iloc[-1] / c.iloc[-n] - 1)

    mom_1m = _mom(21)
    mom_3m = _mom(63)
    mom_6m = _mom(126)

    # RS_126: 섹터 6M ÷ 기준 시장 6M
    rs_126: float | None = None
    if mom_6m is not None and ref_return is not None and abs(ref_return) > 1e-9:
        rs_126 = _safe_float(mom_6m / ref_return)

    # 연율 변동성
    vol_ann = _compute_vol_ann(c.tail(253))

    # 위험 조정 모멘텀
    risk_adj_mom: float | None = None
    if mom_6m is not None and vol_ann and vol_ann > 1e-9:
        risk_adj_mom = _safe_float(mom_6m / vol_ann)

    # 거래대금 추세 (MA20 ÷ MA60)
    value_trend: float | None = None
    if "trading_value" in df.columns and len(df) >= 60:
        tv = df["trading_value"]
        ma20 = tv.rolling(20).mean().iloc[-1]
        ma60 = tv.rolling(60).mean().iloc[-1]
        if ma60 > 0:
            value_trend = _safe_float(ma20 / ma60)

    # 52주 고가 대비
    high_52w_ratio: float | None = None
    if len(c) >= 252:
        hi = c.tail(252).max()
        if hi > 0:
            high_52w_ratio = _safe_float(c.iloc[-1] / hi)

    # MDD_252
    mdd_252 = _compute_mdd(c.tail(252)) if len(c) >= 2 else None

    # 섹터 밸류(PER/PBR/배당) 중앙값은 업종 구성종목 API 가 현재 pykrx 버전에서
    # 빈 값을 반환해 산출 불가하므로 계산하지 않는다(응답 스키마에서도 제외).

    return SectorMetric(
        name=name,
        market=mkt,
        mom_1m=mom_1m,
        mom_3m=mom_3m,
        mom_6m=mom_6m,
        rs_126=rs_126,
        vol_ann=vol_ann,
        risk_adj_mom=risk_adj_mom,
        value_trend=value_trend,
        high_52w_ratio=high_52w_ratio,
        mdd_252=mdd_252,
    )


# ─────────────────────── 종목 지표 계산 ───────────────────────


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
    # market tag는 fund_df에서 가져옴
    merged = cap_df.copy()

    # 시장 태그: fund_df.market 컬럼 병합
    if not fund_df.empty and "market" in fund_df.columns:
        merged = merged.join(fund_df[["PER", "PBR", "DIV", "market"]], how="left")
    else:
        merged["market"] = market if market != "ALL" else "KOSPI"
        for col in ["PER", "PBR", "DIV"]:
            merged[col] = np.nan

    # 1-day 종가 및 등락률
    if not pc_1d.empty:
        if "종가" in pc_1d.columns:
            merged["price_close"] = pc_1d["종가"]
        if "등락률" in pc_1d.columns:
            merged["change_rate"] = _pct_dec(pc_1d["등락률"])

    # 21-day 모멘텀 및 거래대금
    if not pc_21d.empty:
        if "등락률" in pc_21d.columns:
            merged["mom_1m"] = _pct_dec(pc_21d["등락률"])
        if "거래대금" in pc_21d.columns:
            # 총 거래대금 / 20 = 일평균 (약 20 영업일)
            merged["total_tv_21d"] = pc_21d["거래대금"]
            merged["avg_value_20"] = pc_21d["거래대금"] / 20.0

    # 63-day 모멘텀
    if not pc_63d.empty and "등락률" in pc_63d.columns:
        merged["mom_3m"] = _pct_dec(pc_63d["등락률"])

    # 126-day 모멘텀
    if not pc_126d.empty and "등락률" in pc_126d.columns:
        merged["mom_6m"] = _pct_dec(pc_126d["등락률"])

    # 12-1 모멘텀
    if not pc_12_1.empty and "등락률" in pc_12_1.columns:
        merged["mom_12_1"] = _pct_dec(pc_12_1["등락률"])

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

        # MDD_252
        if len(close) >= 2:
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


def _build_krx_name_map(*frames: pd.DataFrame) -> dict[str, str]:
    """price_change 프레임들의 '종목명' 컬럼에서 코드→이름 맵을 만든다.

    이미 조회한 데이터(get_market_price_change)를 재활용하므로 추가 KRX 호출이
    없다. 여러 프레임을 넘기면 먼저 채워진 값을 유지한다(먼저 온 프레임 우선).
    코드는 6자리 zero-fill 로 정규화한다.
    """
    names: dict[str, str] = {}
    for df in frames:
        if df is None or df.empty or "종목명" not in df.columns:
            continue
        for code, name in df["종목명"].items():
            key = str(code).zfill(6)
            if key not in names:
                text = str(name).strip()
                if text:
                    names[key] = text
    return names


def _build_name_map() -> dict[str, str]:
    """symbol 카탈로그에서 코드→이름 딕셔너리를 반환한다.

    blocking 함수이지만 프로세스 캐시이므로 최초 1회만 빌드된다.
    """
    try:
        catalog = get_catalog()
        return {item["code"]: item["name"] for item in catalog}
    except Exception:
        logger.warning("종목 이름 카탈로그 로드 실패", exc_info=True)
        return {}


def _is_nan(val: Any) -> bool:
    """None 또는 NaN 여부 확인."""
    if val is None:
        return True
    try:
        return not np.isfinite(float(val))
    except (TypeError, ValueError):
        return True


def _safe_bool(val: Any) -> bool | None:
    """NaN/None을 None으로 변환해 bool을 반환한다."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    return bool(val)
