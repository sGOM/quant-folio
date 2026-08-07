"""멀티팩터 스코어링 — 윈저라이징 z-score, 사이즈 중립화, 종합점수, 유니버스 점수.

카테고리(모멘텀/밸류/저변동/퀄리티/성장) z-score 를 합성해 종합점수를 산출한다.
`compute_universe_scores` 는 리밸런싱 selection.method="score" 전용 진입점으로,
fetch(등락률/펀더멘털)·stocks(기술지표)·opendart(재무)를 조합한다.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

from app.services.data.errors import DataSourceError
from app.services.data.loader import bounded_socket_timeout
from app.services.metrics.common import _approx_start, _is_nan, _ymd
from app.services.metrics.fetch import (
    _fetch_fundamentals,
    _fetch_market_cap,
    _fetch_net_purchases,
    _fetch_price_change,
)

logger = logging.getLogger("app.services.metrics")


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
#: residual_momentum 기본 0.0 — 잔차(베타 조정) 모멘텀 옵트인 대체 팩터(컬럼 부재 시 중립).
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    "momentum": 0.4, "value": 0.3, "lowvol": 0.3, "quality": 0.0, "growth": 0.0,
    "flow": 0.0, "residual_momentum": 0.0, "pead": 0.0,
}

#: PEAD(실적 서프라이즈 드리프트) 기본 파라미터 — SUE 표준화 lookback 분기수.
#: OpenDART 정기공시 접수일 기준 PIT 로 단일분기 순이익 YoY 서프라이즈를 계산해
#: 최근 lookback 분기 표준편차로 표준화한다(opendart.pead_sue_by_symbol).
PEAD_DEFAULT_LOOKBACK_Q = 8

#: 잔차 모멘텀(Residual Momentum, Blitz·Huij·Martens 2011) 기본 파라미터(월 단위).
#: reg_window: 시장 회귀 롤링 윈도우(월). mom_window: 형성창(월, 최근 skip 제외).
#: skip: 최근 단기 반전(reversal) 제거용 스킵 개월. min_obs: 회귀 최소 월 관측치.
RESID_MOM_DEFAULT_REG_WINDOW = 36
RESID_MOM_DEFAULT_WINDOW = 11
RESID_MOM_DEFAULT_SKIP = 1
RESID_MOM_MIN_OBS = 24


def compute_residual_momentum_panel(
    close_panel: pd.DataFrame,
    market_close: pd.Series,
    as_of: date,
    codes: list[str] | None = None,
    reg_window: int = RESID_MOM_DEFAULT_REG_WINDOW,
    mom_window: int = RESID_MOM_DEFAULT_WINDOW,
    skip: int = RESID_MOM_DEFAULT_SKIP,
    min_obs: int = RESID_MOM_MIN_OBS,
) -> pd.Series:
    """잔차(베타 조정) 모멘텀 원값 resid_mom 을 계산한다(Blitz·Huij·Martens 2011).

    개별 종목 월수익률을 시장(KOSPI200 지수) 월수익률에 회귀(as_of 이하 reg_window 개월
    롤링)해 시장·베타 성분을 제거한 잔차 시계열을 얻고, 형성창(최근 mom_window 개월,
    최근 skip 개월 제외) 잔차의 평균/표준편차(잔차 정보비율)를 모멘텀 스코어로 삼는다.
    std 로 나눠 고변동 종목의 과대 노출을 억제하는 것이 원시 모멘텀 대비 핵심 차이다.

    설계 근거: id=23 저베타 방어형에선 원시 모멘텀이 사실상 베타/변동성 베팅으로 변질돼
    IR 이 음(−0.18)이었다(팩터 IC/IR 분석). 시장 성분을 회귀로 걷어낸 잔차만 누적하면
    그 오염을 구조적으로 제거할 수 있는지 검증하기 위한 옵트인 대체 팩터.

    미래참조 방지: as_of 이하 확정 종가만 사용한다(월말 리샘플·회귀·형성 모두 as_of 컷).
    사이즈(로그시총) 조정은 이 시계열 회귀 대신 스코어러의 크로스섹션 사이즈 중립화
    (selection.neutralize="size")로 처리한다 — 시계열 회귀에 SMB 수익 시계열이 없어도
    되고(외부 데이터 불요), 사이즈는 본질적으로 크로스섹션 특성이기 때문이다.

    :param close_panel: index=일자, columns=종목코드, 값=종가(워밍업 포함, 최소 reg_window
        개월 이상 과거를 포함해야 함).
    :param market_close: 시장 지수(KOSPI200) 종가 Series(동일 구간).
    :param codes: 계산 대상 종목(None 이면 close_panel 전 컬럼).
    :return: index=종목코드, 값=resid_mom(float). 관측치 부족 종목은 제외(중립 폴백 대상).
    """
    if close_panel is None or close_panel.empty or market_close is None or market_close.empty:
        return pd.Series(dtype="float64")

    as_of_ts = pd.Timestamp(pd.Timestamp(as_of).date())

    def _monthly_returns(obj: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
        # tz-naive 정규화 → as_of 컷 → 월(period) 말 종가 → 월수익률.
        idx = obj.index
        if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
            idx = idx.tz_localize(None)
        obj = obj.copy()
        obj.index = pd.DatetimeIndex(idx).normalize()
        obj = obj[obj.index <= as_of_ts]
        if obj.empty:
            return obj
        monthly = obj.groupby(obj.index.to_period("M")).last()
        # fill_method=None: 데이터 공백(상폐·거래정지) 월의 수익률을 pad 로 날조하지 않고
        # NaN 으로 남긴다(회귀 표본에서 자연 제외 — notna 마스크가 처리). 선행 NaN(미상장)도 유지.
        return monthly.pct_change(fill_method=None)

    stock_ret = _monthly_returns(close_panel)
    mkt_ret = _monthly_returns(market_close)
    if isinstance(stock_ret, pd.Series):
        stock_ret = stock_ret.to_frame()
    if stock_ret.empty or mkt_ret.empty:
        return pd.Series(dtype="float64")

    # 회귀 윈도우: 형성창(mom_window+skip)보다 넉넉히 길어야 잔차 추정이 안정적이다.
    need = mom_window + skip
    reg_window = max(reg_window, need)

    # 최근 reg_window 개월로 제한(as_of 기준 롤링). 시장·종목 공통 월 인덱스에 정렬.
    mkt = mkt_ret.dropna()
    mkt = mkt.iloc[-reg_window:]
    if len(mkt) < min_obs:
        return pd.Series(dtype="float64")
    x = mkt.to_numpy(dtype=float)

    target_codes = list(codes) if codes is not None else list(stock_ret.columns)
    norm_map = {str(c).strip().zfill(6): c for c in stock_ret.columns}
    out: dict[str, float] = {}
    for code in target_codes:
        col = code if code in stock_ret.columns else norm_map.get(str(code).strip().zfill(6))
        if col is None:
            continue
        y_full = stock_ret[col].reindex(mkt.index)
        m = y_full.notna()
        n = int(m.sum())
        if n < min_obs:
            continue
        yv = y_full[m].to_numpy(dtype=float)
        xv = x[m.to_numpy()]
        xvar = float(((xv - xv.mean()) ** 2).sum())
        if not (xvar > 0):
            continue
        beta = float(((xv - xv.mean()) * (yv - yv.mean())).sum() / xvar)
        alpha = float(yv.mean() - beta * xv.mean())
        resid = yv - alpha - beta * xv  # 시장·베타 성분 제거 잔차(정렬 순서 = mkt.index)
        # 형성창: 최근 skip 개월 제외 후 mom_window 개월. resid 는 유효월만 남았으므로
        # 인덱스 기준 최근값에서 잘라낸다(월 결측이 섞여도 순서는 보존).
        tail = resid[-(mom_window + skip):]
        formation = tail[:-skip] if skip > 0 else tail
        if len(formation) < 2:
            continue
        mu = float(formation.mean())
        sd = float(formation.std(ddof=1))
        out[str(code).strip().zfill(6)] = mu / sd if sd > 0 else mu
    return pd.Series(out, dtype="float64")


def compute_residual_momentum(
    codes: list[str],
    as_of: date,
    reg_window: int = RESID_MOM_DEFAULT_REG_WINDOW,
    mom_window: int = RESID_MOM_DEFAULT_WINDOW,
    skip: int = RESID_MOM_DEFAULT_SKIP,
    market_ticker: str = "1028",
) -> pd.Series:
    """실거래(compute_universe_scores) 경로용 잔차 모멘텀: 종가·KOSPI200 지수를 조회해 계산.

    각 종목 종가 이력(reg_window+2 개월 버퍼)과 시장 지수(market_ticker, 기본 KOSPI200
    =1028) 종가를 조회해 compute_residual_momentum_panel 로 위임한다. 외부 데이터는
    종가+지수뿐이며(벤치마크·레짐이 이미 쓰는 소스 재사용), 두 조회는 저하 방식이 다르다:

    - 시장 지수(_fetch_index_ohlcv)는 잔차 계산의 기준선이라 조회 실패는 그대로
      전파한다(리밸런싱 경로 기준). 조회는 성공했지만 실제로 데이터가 없는 경우만
      빈 Series 를 반환한다(전량 계산 불가 신호, 호출자가 스킵 판단).
    - 개별 종목 종가(load_ohlcv)는 실패한 종목만 결과에서 빠지고(중립 폴백 대상)
      나머지 종목으로 계산을 이어간다 — try/except 로 흡수한다.

    미래참조 방지: as_of 이하 종가만 조회·사용한다.
    """
    if not codes:
        return pd.Series(dtype="float64")
    from app.services.data.loader import load_ohlcv
    from app.services.metrics.fetch import _fetch_index_ohlcv

    norm_codes = [str(c).strip().zfill(6) for c in codes]
    # reg_window 개월 ≈ reg_window×22 거래일. 형성창·버퍼 포함 넉넉히 조회.
    lookback_days = int((reg_window + 3) * 22)
    start_ymd = _ymd(_approx_start(as_of, lookback_days, buffer=45))
    as_of_ymd = _ymd(as_of)

    with bounded_socket_timeout(30):
        idx_df = _fetch_index_ohlcv(start_ymd, as_of_ymd, market_ticker)
        if idx_df is None or idx_df.empty or "close" not in idx_df.columns:
            return pd.Series(dtype="float64")  # 시장 지수 없으면 계산 불가(전량 실패 신호)
        market_close = idx_df["close"].astype(float)

        columns: dict[str, pd.Series] = {}
        for code in norm_codes:
            try:
                df = load_ohlcv(code, start_ymd, as_of_ymd)
            except Exception:  # noqa: BLE001
                continue
            if df is None or df.empty or "close" not in df.columns:
                continue
            columns[code] = df["close"].astype(float)

    if not columns:
        return pd.Series(dtype="float64")
    panel = pd.DataFrame(columns)
    return compute_residual_momentum_panel(
        panel, market_close, as_of, codes=norm_codes,
        reg_window=reg_window, mom_window=mom_window, skip=skip,
    )

#: 수급(flow) 팩터 기본 파라미터 — 누적창(거래일)·정규화 분모.
#: financial-expert 설계상 60~120일 누적 지속(persistence) 신호. 워크포워드 확정 전
#: 잠정 기본값(90일·시가총액). window 는 거래일 수, denom 은 "mcap"(시가총액) 또는
#: "value"(동일창 누적 거래대금).
FLOW_DEFAULT_WINDOW = 90
FLOW_DEFAULT_DENOM = "mcap"


def compute_flow_norm(
    codes: list[str],
    as_of: date,
    window: int = FLOW_DEFAULT_WINDOW,
    denom: str = FLOW_DEFAULT_DENOM,
) -> pd.Series:
    """수급(flow) 팩터 원값 flow_norm 을 계산한다(외국인+기관 누적 순매수 / 정규화 분모).

    지속(persistence) accumulation 신호: [as_of-window, as_of] 구간 누적 외국인+기관
    순매수 '대금'을 시가총액(denom="mcap") 또는 동일 구간 누적 거래대금(denom="value")으로
    나눠 스케일-프리하게 만든다. 회전율 억제를 위해 단기 반전이 아닌 수개월 누적을 본다.

    미래참조 방지: as_of 이하 확정 데이터만 사용한다(호출자가 직전 확정 영업일로 넘긴다).
    조회 실패(전량이든 부분이든)는 예외로 전파한다(호출자가 리밸런싱 스킵 판단) —
    flow 는 factor_weights 로 명시 요청된 팩터라 조용히 빠지면 §47 사고 패턴이
    재현된다. 빈 Series 가 반환되는 경우는 순매수 조회 자체는 성공했지만 실적이 없는
    (로컬 스토어가 0행을 확정 기록한) 경우뿐이다 — 이때는 분모(시총/거래대금) 조회를
    아예 하지 않는다(순매수가 없으면 분모도 쓰이지 않으므로 불필요한 외부 조회를
    피한다). window 는 거래일 수 → _approx_start 로 달력일 근사.

    :return: index=종목코드(6자리), 값=flow_norm(float). 계산 불가 종목은 제외.
    """
    if not codes:
        return pd.Series(dtype="float64")
    norm_codes = [str(c).strip().zfill(6) for c in codes]
    mkts = ["KOSPI", "KOSDAQ"]
    as_of_ymd = _ymd(as_of)
    start_ymd = _ymd(_approx_start(as_of, window))

    with bounded_socket_timeout(30):
        npf = _fetch_net_purchases(start_ymd, as_of_ymd, mkts)
        if npf is None or npf.empty:
            return pd.Series(dtype="float64")  # 순매수 실적 없음 — 분모 조회 불필요
        if denom == "value":
            denom_df = _fetch_price_change(start_ymd, as_of_ymd, mkts)  # 거래대금=구간 누적
        else:
            denom_df = _fetch_market_cap(as_of_ymd, mkts)  # 시가총액=시점값

    npf = npf.copy()
    npf.index = npf.index.astype(str).str.zfill(6)
    net = npf["net_buy_value"].reindex(norm_codes)

    denom_series: pd.Series | None = None
    if denom_df is not None and not denom_df.empty:
        denom_df = denom_df.copy()
        denom_df.index = denom_df.index.astype(str).str.zfill(6)
        col = "거래대금" if denom == "value" else "시가총액"
        if col in denom_df.columns:
            denom_series = pd.to_numeric(
                denom_df[col], errors="coerce"
            ).reindex(norm_codes)

    out: dict[str, float] = {}
    for code in norm_codes:
        nv = net.get(code)
        if nv is None or _is_nan(nv):
            continue
        dv = denom_series.get(code) if denom_series is not None else None
        if dv is None or _is_nan(dv) or dv <= 0:
            continue
        out[code] = float(nv) / float(dv)
    return pd.Series(out, dtype="float64")


def compute_pead_sue(
    codes: list[str],
    as_of: date,
    lookback_q: int = PEAD_DEFAULT_LOOKBACK_Q,
) -> pd.Series:
    """PEAD 팩터 원값 pead_sue 를 계산한다(표준화 기대외 이익, OpenDART 접수일 PIT).

    opendart.pead_sue_by_symbol 로 위임한다 — 각 종목의 실제 정기공시 접수일(rcept_dt)
    기준으로 as_of 시점에 이미 공시된 분기 실적만 사용해(미래참조 없음) 단일분기 순이익
    YoY 서프라이즈를 최근 lookback_q 분기 표준편차로 표준화한다. 컨센서스 부재로 기대치는
    계절적 랜덤워크(전년 동기) 프록시를 쓴다. 조회 실패/표본 부족 종목은 결과에서 빠지고
    (중립 폴백 대상), 전량 실패면 빈 Series 를 반환한다(호출자가 리밸런싱 스킵 판단).

    :return: index=종목코드(6자리), 값=pead_sue(float). 계산 불가 종목은 제외.
    """
    if not codes:
        return pd.Series(dtype="float64")
    from app.services.data import opendart

    norm_codes = [str(c).strip().zfill(6) for c in codes]
    try:
        sue = opendart.pead_sue_by_symbol(norm_codes, as_of, lookback_q=lookback_q)
    except Exception as e:  # noqa: BLE001
        logger.warning("PEAD(SUE) 팩터 조회 실패 — 중립 처리: %s", e)
        return pd.Series(dtype="float64")
    if not sue:
        return pd.Series(dtype="float64")
    return pd.Series(sue, dtype="float64")


def _neutralize_size(df: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    """각 팩터 카테고리 z-score 를 로그 시가총액 축에 대해 직교화한다(사이즈 중립화, P1-3).

    residual = s − β·x, β = Σ(s·x)/Σ(x²), x = 표준화(로그 시총, 평균 0). 팩터 점수에서
    선형 사이즈 노출을 제거해 '팩터가 사실상 대형/소형주 베팅으로 변질'되는 것을 막는다.
    df["market_cap"](원) 이 없거나 유효 표본<5 면 원본을 그대로 둔다(중립화 생략). NaN 인
    팩터/시총 행은 보존한다(랭킹 제외 로직 유지). df 를 제자리 수정하고 반환한다.
    """
    cap = df.get("market_cap")
    if cap is None:
        return df
    logcap = np.log(cap.where(cap > 0).astype(float))
    sd = logcap.std(ddof=0)
    if not (sd > 0):
        return df
    x = (logcap - logcap.mean()) / sd   # 표준화 로그 시총(평균 0)
    valid = x.notna()
    if int(valid.sum()) < 5:
        return df
    xv = x[valid]
    if not (float((xv ** 2).sum()) > 0):
        return df
    for col in factor_cols:
        if col not in df.columns:
            continue
        s = df[col]
        # 팩터·시총 모두 유효한 행에서만 β 추정·잔차화(팩터 NaN 행은 건드리지 않음).
        m = valid & s.notna()
        if int(m.sum()) < 5:
            continue
        sv = s[m]
        xm = x[m]
        # 무절편 OLS 사영 β = Σ_m(s·x)/Σ_m(x²). 분자·분모를 반드시 동일 표본(m)으로
        # 계산해야 편향이 없다. denom 을 valid 전체(팩터 NaN 포함)의 Σx² 로 잡으면
        # m ⊆ valid 라 분모가 과대 → β 가 0쪽으로 축소 → 사이즈 노출이 일부만 제거되고
        # 소형주 틸트가 잔차에 남는다.
        denom_m = float((xm ** 2).sum())
        if not (denom_m > 0):
            continue
        beta = float((sv * xm).sum() / denom_m)
        df.loc[m, col] = sv - beta * xm
    return df


def _neutralize_sector(df: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    """각 팩터 카테고리 z-score 에서 섹터(업종) 평균을 제거한다(섹터 중립화, §20).

    residual = s − mean(s | sector). 범주형 축이라 사이즈처럼 연속 회귀를 쓰지 않고
    섹터별 그룹 평균을 빼는 demean 방식을 쓴다 — 그룹 내 상대 순위만 남겨, 팩터
    점수가 특정 업종 쏠림(예: 저변동=유틸리티/통신)으로 변질되는 것을 막는다.
    df["sector"](업종명 문자열) 이 없으면 원본을 그대로 둔다(중립화 생략). 표본이
    1개뿐인 섹터는 평균을 빼면 자기 자신이 0이 되어 정보가 사라지므로 건드리지
    않는다(그 섹터는 원값 유지). NaN 인 팩터/섹터 행은 보존한다. df 를 제자리
    수정하고 반환한다.
    """
    sector = df.get("sector")
    if sector is None:
        return df
    valid = sector.notna()
    if int(valid.sum()) < 5:
        return df
    for col in factor_cols:
        if col not in df.columns:
            continue
        s = df[col]
        m = valid & s.notna()
        if int(m.sum()) < 5:
            continue
        grp = sector[m]
        sv = s[m]
        group_mean = sv.groupby(grp).transform("mean")
        group_size = sv.groupby(grp).transform("size")
        # 표본 1개뿐인 섹터는 자기 평균=자기 자신이라 demean 하면 0으로 소거된다 —
        # 정보 소실 방지를 위해 그 그룹만 원값을 보존한다.
        residual = (sv - group_mean).where(group_size > 1, sv)
        df.loc[m, col] = residual
    return df


def _compute_stock_scores(
    df: pd.DataFrame,
    weights: dict[str, float] | None = None,
    neutralize: str = "none",
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

    # 컬럼 접근/z-score 헬퍼 — 없는 컬럼은 전 종목 결측(NaN) 시리즈로 대체.
    nan_series = pd.Series(np.nan, index=df.index)

    def _col(name: str) -> pd.Series:
        return df.get(name, nan_series)

    def _zc(name: str, *, invert: bool = False, fill: float = 0.0) -> pd.Series:
        return _winsorize_zscore(_col(name), invert=invert).fillna(fill)

    # ── 모멘텀 카테고리 z-score ──
    z_m1 = _winsorize_zscore(_col("mom_1m"))
    z_m3 = _winsorize_zscore(_col("mom_3m"))
    z_m6 = _winsorize_zscore(_col("mom_6m"))
    z_h52 = _winsorize_zscore(_col("high_52w_ratio"))

    # 각 z-score 결측은 0(중립)으로 채운 뒤 평균 → 전체 결측이면 NaN
    mom_stack = pd.concat([
        z_m1.fillna(0), z_m3.fillna(0), z_m6.fillna(0), z_h52.fillna(0)
    ], axis=1)
    score_momentum = mom_stack.mean(axis=1)
    # mom_6m 결측 종목은 모멘텀 점수 NaN → 랭킹 제외
    score_momentum = score_momentum.where(_col("mom_6m").notna())

    # ── 밸류에이션 카테고리 z-score ──
    # 컬럼명은 대문자 PER/PBR/DIV 로 통일한다. 데이터 생성부(_fetch_fundamentals,
    # compute_stocks, compute_universe_scores)가 모두 대문자로 채우므로 여기서도
    # 대문자로 읽어야 밸류 팩터가 실제로 반영된다(소문자로 읽으면 항상 결측→중립 0
    # 이 되어 밸류 가중치가 통째로 무시되는 버그가 있었다).
    per_col = _col("PER")
    per_pos = per_col.where(per_col > 0)   # 음수 PER → NaN
    z_per = _winsorize_zscore(per_pos, invert=True).fillna(0.0)   # 결측→중립
    z_pbr = _zc("PBR", invert=True)
    z_div = _zc("DIV")
    score_value = pd.concat([z_per, z_pbr, z_div], axis=1).mean(axis=1)

    # ── 저변동 카테고리 z-score ──
    z_vol = _zc("vol_ann", invert=True)
    # mdd_252는 음수(예: -0.15), 0에 가까울수록 좋음 → z-score 그대로(반전 불필요)
    z_mdd = _zc("mdd_252")
    score_lowvol = pd.concat([z_vol, z_mdd], axis=1).mean(axis=1)

    # ── 퀄리티 카테고리 z-score (OpenDART 재무데이터) ──
    # ROE 높을수록·부채비율 낮을수록·FCF 흑자일수록 우량. FCF 는 원화 절대액이라
    # 크기 편향이 있어 '흑자 여부(1/0)'로 스케일-프리하게 반영한다. 세 컬럼이 모두
    # 없으면(=OpenDART 미배선) score_quality 는 전부 0(중립)이 되어 기존 전략 무영향.
    fcf_col = _col("fcf")
    z_roe = _zc("roe")
    z_debt = _zc("debt_ratio", invert=True)  # 저부채 우량
    fcf_pos = (fcf_col > 0).astype(float).where(fcf_col.notna())   # 흑자=1, 적자=0, 결측 NaN
    z_fcf = _winsorize_zscore(fcf_pos).fillna(0.0)
    z_fscore = _zc("f_score")  # Piotroski F-Score(0~8), 높을수록 우량
    score_quality = pd.concat([z_roe, z_debt, z_fcf, z_fscore], axis=1).mean(axis=1)

    # ── 성장(실적상향) 카테고리 z-score (OpenDART 재무데이터) ──
    # 영업이익·순이익 YoY 성장률(높을수록 우량) + 흑자전환(1/0). 세 컬럼이 모두
    # 없으면(=OpenDART 미배선) score_growth 는 전부 0(중립) → 기존 전략 무영향.
    z_opg = _zc("op_growth")
    z_netg = _zc("net_growth")
    z_turn = _zc("turnaround")
    score_growth = pd.concat([z_opg, z_netg, z_turn], axis=1).mean(axis=1)

    # ── 수급(flow) 카테고리 z-score (외국인+기관 누적 순매수/시총·거래대금) ──
    # 지속(persistence) accumulation 신호. 60~120일 누적 순매수 대금을 시총 또는
    # 거래대금으로 정규화한 flow_norm 을 그대로 z-score 한다(높을수록 순매수 지속 →
    # invert 불필요). flow_norm 컬럼이 없으면(=미배선) score_flow 는 전부 0(중립)이
    # 되어 flow 가중치 0 인 기존 전략은 영향받지 않는다.
    z_flow = _zc("flow_norm")
    score_flow = z_flow

    # ── 잔차(베타 조정) 모멘텀 카테고리 z-score (Blitz·Huij·Martens 2011) ──
    # 시장 회귀 잔차의 형성창 정보비율(resid_mom)을 그대로 z-score 한다(높을수록 우량 →
    # invert 불필요). resid_mom 컬럼이 없으면(=미배선/데이터 부족) score_residual_momentum
    # 은 전부 0(중립)이 되어 residual_momentum 가중치 0 인 기존 전략은 영향받지 않는다.
    # 원시 모멘텀(score_momentum)과 별개 슬롯 — 원시 필드는 그대로 두고 병행하는 옵트인 대체.
    z_resmom = _zc("resid_mom")
    score_residual_momentum = z_resmom

    # ── PEAD(실적 서프라이즈 드리프트) 카테고리 z-score (OpenDART 접수일 PIT) ──
    # 표준화 기대외 이익(SUE, 단일분기 순이익 YoY 서프라이즈/표준편차)을 그대로 z-score
    # 한다(높을수록 양(+)의 서프라이즈 → 상방 드리프트 기대 → invert 불필요). pead_sue
    # 컬럼이 없으면(=미배선/데이터 부족) score_pead 는 전부 0(중립)이 되어 pead 가중치
    # 0 인 기존 전략은 영향받지 않는다.
    z_pead = _zc("pead_sue")
    score_pead = z_pead

    # 카테고리 점수를 프레임에 싣는다(중립화·종합점수 산출의 기준).
    result["score_momentum"] = score_momentum
    result["score_value"] = score_value
    result["score_lowvol"] = score_lowvol
    result["score_quality"] = score_quality
    result["score_growth"] = score_growth
    result["score_flow"] = score_flow
    result["score_residual_momentum"] = score_residual_momentum
    result["score_pead"] = score_pead

    # ── 팩터 중립화(P1-3 사이즈, §20 섹터): 종합 전에 각 카테고리 점수를 지정 축에
    # 직교화한다. "size_sector"는 둘 다 순차 적용(사이즈 먼저 — 연속축 잔차화 후
    # 범주형 demean 이 사이즈 조정된 값 위에서 섹터 쏠림만 추가로 제거한다).
    cat_cols = ["score_momentum", "score_value", "score_lowvol", "score_quality",
                "score_growth", "score_flow", "score_residual_momentum", "score_pead"]
    if neutralize in ("size", "size_sector"):
        result = _neutralize_size(result, cat_cols)
    if neutralize in ("sector", "size_sector"):
        result = _neutralize_sector(result, cat_cols)

    # ── 종합 점수(중립화 후 카테고리 점수로 합성) ──
    score = (
        w.get("momentum", DEFAULT_FACTOR_WEIGHTS["momentum"]) * result["score_momentum"]
        + w.get("value", DEFAULT_FACTOR_WEIGHTS["value"]) * result["score_value"]
        + w.get("lowvol", DEFAULT_FACTOR_WEIGHTS["lowvol"]) * result["score_lowvol"]
        + w.get("quality", DEFAULT_FACTOR_WEIGHTS["quality"]) * result["score_quality"]
        + w.get("growth", DEFAULT_FACTOR_WEIGHTS["growth"]) * result["score_growth"]
        + w.get("flow", DEFAULT_FACTOR_WEIGHTS["flow"]) * result["score_flow"]
        + w.get("residual_momentum", DEFAULT_FACTOR_WEIGHTS["residual_momentum"])
        * result["score_residual_momentum"]
        + w.get("pead", DEFAULT_FACTOR_WEIGHTS["pead"]) * result["score_pead"]
    )
    result["score"] = score.round(4)
    for c in cat_cols:
        result[c] = result[c].round(4)

    return result


def compute_universe_scores(
    symbols: list[str],
    as_of: date,
    factor_weights: dict[str, float] | None = None,
    neutralize: str = "none",
    financial_period: str = "annual",
    flow_window: int = FLOW_DEFAULT_WINDOW,
    flow_denom: str = FLOW_DEFAULT_DENOM,
    resid_mom_reg_window: int = RESID_MOM_DEFAULT_REG_WINDOW,
    resid_mom_window: int = RESID_MOM_DEFAULT_WINDOW,
    resid_mom_skip: int = RESID_MOM_DEFAULT_SKIP,
    pead_lookback_q: int = PEAD_DEFAULT_LOOKBACK_Q,
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
    :param financial_period: "annual"(기본)/"ttm" — 퀄리티 팩터(OpenDART)의 재무 반영
        주기(§8/§3, RebalanceConfig.financial_period). "ttm"이면 분기 TTM 경로로 조회한다.
    :param flow_window: 수급(flow) 팩터 누적창(거래일). flow 가중치>0 일 때만 조회.
    :param flow_denom: 수급 팩터 정규화 분모("mcap"=시가총액, "value"=구간 누적 거래대금).
    :param resid_mom_reg_window: 잔차 모멘텀 시장 회귀 롤링 윈도우(월). residual_momentum
        가중치>0 일 때만 종가+KOSPI200 지수를 조회해 계산한다.
    :param resid_mom_window: 잔차 모멘텀 형성창(월, 최근 skip 제외).
    :param resid_mom_skip: 잔차 모멘텀 최근 스킵 개월(단기 반전 제거).
    :param pead_lookback_q: PEAD SUE 표준화 lookback 분기수. pead 가중치>0 일 때만 OpenDART
        접수일 PIT 로 조회한다(컬럼 부재 시 중립 처리).
    :return: {종목코드: score} dict. 데이터 부족(예: mom_6m 결측)으로 계산 불가한
        종목은 결과에서 제외된다(compute_target_weights 가 자연히 후순위/미선정 처리).
    """
    # _compute_tech_indicators 는 stocks 모듈에 있고, stocks 는 factors._compute_stock_scores
    # 를 최상위 임포트하므로 여기서 최상위 임포트하면 순환이 된다 → 함수 내부 지연 임포트.
    from app.services.metrics.stocks import _compute_tech_indicators

    if not symbols:
        return {}

    codes = [str(s).strip().zfill(6) for s in symbols]
    as_of_ymd = _ymd(as_of)
    mkts = ["KOSPI", "KOSDAQ"]  # universe 종목의 소속 시장을 미리 알 수 없어 양쪽 조회

    # pykrx 내부 호출 다수(get_market_fundamental/get_market_price_change 등)가 자체
    # timeout 을 지정하지 않아, 응답 지연 시 이 함수 전체(최대 종목 수만큼 반복 호출)가
    # 무한 대기할 수 있다. 소켓 레벨로 일괄 방어한다(개별 호출은 각자 try/except 로
    # 이미 예외를 흡수하므로, 타임아웃 예외가 나면 해당 조회만 건너뛰고 계속 진행된다).
    with bounded_socket_timeout(20):
        fund_df = _fetch_fundamentals(as_of_ymd, mkts)
        pc_21d = _fetch_price_change(_ymd(_approx_start(as_of, 21)), as_of_ymd, mkts)
        pc_63d = _fetch_price_change(_ymd(_approx_start(as_of, 63)), as_of_ymd, mkts)
        pc_126d = _fetch_price_change(_ymd(_approx_start(as_of, 126)), as_of_ymd, mkts)

    # 모멘텀 팩터 완전 소멸 방어: 모멘텀 가중치가 유의미한데 3개 기간 등락률 조회가
    # '모두' 비었으면 이는 개별 종목의 정상 NaN 이 아니라 KRX 조회 자체의 일시 장애다.
    # 이 상태로 진행하면 스코어러가 남은 밸류/저변동만으로 재정규화해 에러 없이 전혀
    # 다른 목표 포트폴리오를 산출하고 러너가 그대로 주문한다. 예외를 올려 이번 리밸런싱을
    # 건너뛰면 러너 틱 루프가 잡아 다음 주기에 온전한 팩터로 재시도한다.
    _w = factor_weights or DEFAULT_FACTOR_WEIGHTS
    if float(_w.get("momentum", 0.0) or 0.0) > 0 and pc_21d.empty and pc_63d.empty and pc_126d.empty:
        raise RuntimeError(
            "모멘텀 팩터 데이터 전량 조회 실패(등락률 21/63/126일 모두 빈 응답) — "
            "왜곡된 목표 산출 방지 위해 리밸런싱 건너뜀(다음 주기 재시도)"
        )

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

        qmetrics = opendart.metrics_by_symbol(codes, as_of, use_ttm=financial_period == "ttm")
    except DataSourceError as e:
        # 퀄리티 중립 처리는 설계된 저하다(키 부재 시와 같은 결과). 다만 라이브 엔진이
        # 매 리밸런싱마다 타는 경로라 조용히 넘어가면 '중립 처리'와 구분되지 않으므로
        # ERROR 로 남긴다. 우리 쪽 버그(TypeError 등)는 여기서 삼키지 않고 전파한다.
        logger.error("OpenDART 퀄리티 팩터 조회 실패 — 중립 처리: %s", e)
        qmetrics = {}
    if qmetrics:
        qdf = pd.DataFrame.from_dict(qmetrics, orient="index")
        for col in ("roe", "debt_ratio", "fcf", "f_score",
                    "op_growth", "net_growth", "turnaround"):
            if col in qdf.columns:
                df[col] = qdf[col].reindex(df.index)

    # 사이즈 중립화(P1-3): 시가총액(PIT)을 붙여 스코어러가 로그 시총 축에 직교화한다.
    if neutralize in ("size", "size_sector"):
        try:
            from app.services.data import krx_index
    
            caps = krx_index.market_caps(as_of)
        except DataSourceError as e:
            logger.error("사이즈 중립화용 시가총액 조회 실패 — 중립화 생략: %s", e)
            caps = {}
        if caps:
            df["market_cap"] = pd.Series(
                {c: caps.get(c) for c in df.index}, dtype="float64"
            ).reindex(df.index)

    # 섹터 중립화(§20): 업종명(PIT)을 붙여 스코어러가 섹터 그룹 평균을 제거한다.
    if neutralize in ("sector", "size_sector"):
        try:
            from app.services.data import krx_index
    
            smap = krx_index.sector_map(as_of)
        except DataSourceError as e:
            logger.error("섹터 중립화용 업종분류 조회 실패 — 중립화 생략: %s", e)
            smap = {}
        if smap:
            df["sector"] = pd.Series(
                {c: smap.get(c) for c in df.index}, dtype="object"
            ).reindex(df.index)

    # 수급(flow) 팩터: flow 가중치>0 인 전략에서만 조회(외국인+기관 누적 순매수/분모).
    if float(_w.get("flow", 0.0) or 0.0) > 0:
        try:
            flow = compute_flow_norm(codes, as_of, window=flow_window, denom=flow_denom)
        except Exception as e:  # noqa: BLE001
            logger.warning("수급(flow) 팩터 조회 실패 — 중립 처리: %s", e)
            flow = pd.Series(dtype="float64")
        # 전량 실패 방어(모멘텀 팩터와 동일 취지): flow 가중치가 유의미한데 순매수
        # 조회가 통째로 비었으면 개별 종목 정상 결측이 아니라 KRX 조회 일시 장애다.
        # 이 상태로 진행하면 스코어러가 남은 팩터만으로 재정규화해 왜곡된 목표를
        # 산출하므로, 예외를 올려 이번 리밸런싱을 건너뛰고 다음 주기에 재시도한다.
        if flow.empty:
            raise RuntimeError(
                "수급(flow) 팩터 데이터 전량 조회 실패(외국인+기관 순매수 빈 응답) — "
                "왜곡된 목표 산출 방지 위해 리밸런싱 건너뜀(다음 주기 재시도)"
            )
        df["flow_norm"] = flow.reindex(df.index)

    # 잔차(베타 조정) 모멘텀 팩터: residual_momentum 가중치>0 인 전략에서만 조회
    # (종가+KOSPI200 지수 회귀 잔차). 컬럼 부재 시 스코어러가 중립 처리한다.
    if float(_w.get("residual_momentum", 0.0) or 0.0) > 0:
        try:
            resid_mom = compute_residual_momentum(
                codes, as_of, reg_window=resid_mom_reg_window,
                mom_window=resid_mom_window, skip=resid_mom_skip,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("잔차 모멘텀 팩터 조회 실패 — 중립 처리: %s", e)
            resid_mom = pd.Series(dtype="float64")
        # 전량 실패 방어(모멘텀·flow 와 동일 취지): 가중치가 유의미한데 조회가 통째로
        # 비었으면 개별 종목 정상 결측이 아니라 KRX/지수 조회 일시 장애다. 남은 팩터로
        # 재정규화한 왜곡 목표를 막기 위해 예외를 올려 이번 리밸런싱을 건너뛴다.
        if resid_mom.empty:
            raise RuntimeError(
                "잔차 모멘텀 팩터 데이터 전량 조회 실패(종가/KOSPI200 지수 빈 응답) — "
                "왜곡된 목표 산출 방지 위해 리밸런싱 건너뜀(다음 주기 재시도)"
            )
        df["resid_mom"] = resid_mom.reindex(df.index)

    # PEAD(실적 서프라이즈 드리프트) 팩터: pead 가중치>0 인 전략에서만 조회(OpenDART
    # 정기공시 접수일 PIT 로 단일분기 순이익 YoY 서프라이즈를 표준화). 컬럼 부재 시
    # 스코어러가 중립 처리한다.
    if float(_w.get("pead", 0.0) or 0.0) > 0:
        try:
            pead = compute_pead_sue(codes, as_of, lookback_q=pead_lookback_q)
        except Exception as e:  # noqa: BLE001
            logger.warning("PEAD(SUE) 팩터 조회 실패 — 중립 처리: %s", e)
            pead = pd.Series(dtype="float64")
        # 전량 실패 방어(모멘텀·flow·잔차 모멘텀과 동일 취지): 가중치가 유의미한데 조회가
        # 통째로 비었으면 개별 종목 정상 결측이 아니라 OpenDART 조회 일시 장애/키부재다.
        # 남은 팩터로 재정규화한 왜곡 목표를 막기 위해 예외를 올려 이번 리밸런싱을 건너뛴다.
        if pead.empty:
            raise RuntimeError(
                "PEAD(SUE) 팩터 데이터 전량 조회 실패(정기공시/재무 빈 응답) — "
                "왜곡된 목표 산출 방지 위해 리밸런싱 건너뜀(다음 주기 재시도)"
            )
        df["pead_sue"] = pead.reindex(df.index)

    scored = _compute_stock_scores(df, weights=factor_weights, neutralize=neutralize)
    return {
        code: float(val)
        for code, val in scored["score"].items()
        if not _is_nan(val)
    }
