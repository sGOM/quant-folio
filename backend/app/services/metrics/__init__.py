"""지표 화면 서비스 — 섹터·종목 지표 일괄 계산 및 캐시 관리.

이 패키지는 과거 단일 모듈(`metrics.py`, 1000+줄)을 책임별 서브모듈로 분할한 것이다.
외부(라우트·엔진·스크리너·추천·백테스트·테스트)는 기존과 동일하게
`from app.services.metrics import X` 로 접근한다 — 아래 재노출이 그 경로를 보존한다.

## 서브모듈
- common   : 영업일/날짜 변환, JSON-safe 숫자, MDD·변동성, 시장 파싱
- fetch    : pykrx 일괄 조회(펀더멘털·시총·등락률·업종지수) + 펀더멘털 LRU 캐시
- factors  : 윈저라이징 z-score·사이즈 중립화·종합점수·유니버스 점수
- sectors  : 업종지수 지표(모멘텀·RS·변동성)
- stocks   : 종목 지표 4단계 파이프라인 + 기술지표
- names    : 종목 코드→이름 매핑

## 데이터 소스·캐싱·한계
- 모든 pykrx/FDR 호출은 블로킹이므로 호출자(라우트)가 asyncio.to_thread 로 실행.
- 결과는 as_of + market 키로 Redis 에 캐시 (TTL ≈ 6시간).
- 미래참조 방지: as_of는 직전 확정 영업일(당일 종가 미확정 방지)로 고정한다.
- 생존편향: pykrx는 현재 상장 종목 기준으로 과거 데이터를 제공한다.
"""
from __future__ import annotations

from app.services.metrics.common import (
    _approx_start,
    _compute_mdd,
    _compute_vol_ann,
    _is_nan,
    _last_business_day,
    _mkts,
    _pct_dec,
    _prev_business_day,
    _safe_bool,
    _safe_float,
    _ymd,
    logger,
)
from app.services.metrics.factors import (
    DEFAULT_FACTOR_WEIGHTS,
    _compute_stock_scores,
    _neutralize_size,
    _winsorize_zscore,
    compute_universe_scores,
)
from app.services.metrics.fetch import (
    _FUND_CACHE,
    _FUND_CACHE_MAX,
    _fetch_fundamentals,
    _fetch_index_ohlcv,
    _fetch_index_tickers,
    _fetch_market_cap,
    _fetch_market_ohlcv_snapshot,
    _fetch_price_change,
    _get_index_name,
)
from app.services.metrics.names import _build_krx_name_map, _build_name_map
from app.services.metrics.panic import (
    PANIC_CACHE_TTL,
    compute_panic,
)
from app.services.metrics.sectors import (
    SECTORS_CACHE_TTL,
    _KOSDAQ_REF_TICKER,
    _KOSPI_REF_TICKER,
    _compute_one_sector,
    compute_sectors,
)
from app.services.metrics.stocks import (
    DEFAULT_MIN_MCAP,
    DEFAULT_MIN_VALUE,
    STOCKS_CACHE_TTL,
    _MAX_CANDIDATES,
    _compute_tech_indicators,
    compute_stocks,
)

__all__ = [
    # common
    "_approx_start", "_compute_mdd", "_compute_vol_ann", "_is_nan",
    "_last_business_day", "_mkts", "_pct_dec", "_prev_business_day",
    "_safe_bool", "_safe_float", "_ymd", "logger",
    # fetch
    "_FUND_CACHE", "_FUND_CACHE_MAX", "_fetch_fundamentals", "_fetch_index_ohlcv",
    "_fetch_index_tickers", "_fetch_market_cap", "_fetch_market_ohlcv_snapshot",
    "_fetch_price_change", "_get_index_name",
    # factors
    "DEFAULT_FACTOR_WEIGHTS", "_compute_stock_scores", "_neutralize_size",
    "_winsorize_zscore", "compute_universe_scores",
    # sectors
    "SECTORS_CACHE_TTL", "_KOSDAQ_REF_TICKER", "_KOSPI_REF_TICKER",
    "_compute_one_sector", "compute_sectors",
    # stocks
    "DEFAULT_MIN_MCAP", "DEFAULT_MIN_VALUE", "STOCKS_CACHE_TTL", "_MAX_CANDIDATES",
    "_compute_tech_indicators", "compute_stocks",
    # names
    "_build_krx_name_map", "_build_name_map",
    # panic
    "PANIC_CACHE_TTL", "compute_panic",
]
