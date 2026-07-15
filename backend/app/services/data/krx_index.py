"""KRX 지수 구성종목(시점별 멤버십) 클라이언트.

목적: 백테스트의 생존편향(survivorship bias)을 제거하기 위한 **시점별 지수 구성종목**
공급. "오늘 살아남은 종목"이 아니라 각 과거 시점에 실제로 지수(예: KOSPI200)에
속했던 종목 집합을 반환하므로, 편출·상장폐지된 종목까지 후보풀에 포함해 공정하게
검증할 수 있다.

데이터 소스: KRX 정보데이터시스템(data.krx.co.kr) MDC. 이 포털은 회원 로그인을
요구하며(미인증 요청은 "LOGOUT" 반환), pykrx 1.2.x 가 KRX_ID/KRX_PW 환경변수로
로그인 세션을 관리한다(app.core.config 가 시크릿에서 주입). 여기서는 그 인증
세션을 재사용해 구성종목 JSON(MDCSTAT00601)을 직접 조회한다.

설계 원칙(opendart.py 와 동일):
- 블로킹(sync) 함수 — 호출부가 run_in_threadpool 로 실행.
- 실패/미인증/무자료 시 예외 대신 빈 리스트 반환(호출부가 폴백하도록).
- 시점별 결과를 모듈 캐시(_MEMBERS_CACHE)에 저장(같은 날짜 반복조회 방지).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from app.services.data.loader import bounded_socket_timeout

logger = logging.getLogger(__name__)

_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_BLD_INDEX_CONSTITUENTS = "dbms/MDC/STAT/standard/MDCSTAT00601"
# 종목 검색 finder(전 상장종목 코드·한글명). 날짜 비의존 — 시스템 시계가 미래여도 동작.
_BLD_STOCK_FINDER = "dbms/comm/finder/finder_stkisu"
_MARKET_BY_CODE = {"STK": "KOSPI", "KSQ": "KOSDAQ", "KNX": "KONEX"}
# 전종목 시가총액(MDCSTAT01501). 명시적 trdDd 로 조회 — 과거일이면 그날 종가 기준 시총.
_BLD_MARKET_CAP = "dbms/MDC/STAT/standard/MDCSTAT01501"
# 업종분류현황(MDCSTAT03901). 시장(mktId)별 전종목의 업종명(IDX_IND_NM)을 한 번에 반환.
# 한 시장당 요청 1회로 전종목 업종을 얻어 섹터 집중 한도(risk_layer.max_sector_pct)에 쓴다.
_BLD_SECTOR = "dbms/MDC/STAT/standard/MDCSTAT03901"

# 주요 지수의 MDC 파라미터(indIdx/indIdx2). KOSPI200 = 1/028.
INDEX_PARAMS: dict[str, dict[str, str]] = {
    "KOSPI200": {"indIdx": "1", "indIdx2": "028"},
    "KOSPI100": {"indIdx": "1", "indIdx2": "034"},
    "KRX300": {"indIdx": "5", "indIdx2": "300"},
}

# (index, YYYYMMDD) -> list[str]
_MEMBERS_CACHE: dict[tuple[str, str], list[str]] = {}

# 전 상장종목 목록 캐시(성공 시에만 채움 — 실패를 캐시하지 않아 다음 호출에 재시도).
_STOCKS_CACHE: list[dict[str, str]] | None = None

# YYYYMMDD -> {code: 시가총액(원)}. 시점별 시총(유동성 필터용) 캐시.
_MKTCAP_CACHE: dict[str, dict[str, int]] = {}

# {code: 업종명}. 업종 분류는 (PIT 시총·구성종목과 달리) 사실상 정적이라 프로세스 내
# 1회만 로드해 재사용한다(성공 시에만 채움 — 실패를 캐시하면 섹터 한도가 조용히 무력화됨).
_SECTOR_CACHE: dict[str, str] | None = None


def _session():
    """pykrx 인증 KRX 세션을 반환한다(미설정/실패 시 None).

    app.core.config 로드로 KRX_ID/KRX_PW 가 os.environ 에 주입돼 있어야 한다.
    """
    try:
        from pykrx.website.comm import auth
    except Exception:  # noqa: BLE001
        return None
    # 이미 전역 세션이 유효하면 재사용, 아니면 신규 로그인
    sess = getattr(auth, "_auth_session", None)
    if sess is not None and getattr(sess, "is_valid", lambda: False)():
        return sess
    try:
        # build_krx_session() 은 로그인 로그(로그인 시도/완료 print) 이후에도 내부적으로
        # 추가 요청을 할 수 있는데, 그 경로엔 우리가 손댈 수 없는 timeout 미지정 호출이
        # 있을 수 있다(실제로 로그인 완료 로그 이후 응답 없이 멈추는 현상을 관측함).
        # 소켓 레벨로 강제 타임아웃을 걸어 무한 대기를 방지한다.
        with bounded_socket_timeout(20):
            return auth.build_krx_session()
    except Exception as e:  # noqa: BLE001
        logger.warning("KRX 인증 세션 생성 실패: %s", e)
        return None


def index_members(as_of: date, index: str = "KOSPI200") -> list[str]:
    """as_of 시점에 index 를 구성하던 종목코드(6자리) 목록. 실패 시 빈 리스트.

    :param as_of: 조회 기준일. 주말·휴장일이면 KRX 가 빈 응답을 주므로, 최대 6일 전까지
        직전 영업일로 스냅해 그 시점 구성을 반환한다(PIT 안전 — 미래일로는 가지 않음).
    :param index: INDEX_PARAMS 의 키(기본 KOSPI200).
    """
    params = INDEX_PARAMS.get(index)
    if params is None:
        raise ValueError(f"지원하지 않는 지수: {index} (지원: {list(INDEX_PARAMS)})")
    key = (index, as_of.strftime("%Y%m%d"))
    if key in _MEMBERS_CACHE:
        return _MEMBERS_CACHE[key]

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — %s 구성종목 조회 건너뜀", index)
        return []

    codes: list[str] = []
    # 휴장일 빈 응답 대비 최대 6일(주말+연휴) 직전 영업일까지 스냅.
    for back in range(7):
        dd = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        payload = {
            "bld": _BLD_INDEX_CONSTITUENTS, "locale": "ko_KR",
            "trdDd": dd, "money": "1", "csvxls_isNo": "false", **params,
        }
        try:
            resp = sess.post(_JSON_URL, data=payload, timeout=15)
            rows = resp.json().get("output") or []
        except Exception as e:  # noqa: BLE001
            logger.warning("KRX %s 구성종목 조회 실패(%s): %s", index, dd, e)
            rows = []
        codes = [str(r.get("ISU_SRT_CD")).zfill(6) for r in rows if r.get("ISU_SRT_CD")]
        if codes:
            break

    # 성공 결과만 캐시한다(실패/미인증/일시 장애의 빈 응답을 캐시하면 프로세스 수명
    # 내내 해당 시점 구성이 []로 고착 → 조용히 고정 유니버스로 폴백하는 생존편향 재유입).
    if codes:
        _MEMBERS_CACHE[key] = codes
    return codes


def all_listed_stocks() -> list[dict[str, str]]:
    """전 상장종목 목록 [{code, name, market}] 을 KRX MDC finder 로 조회한다.

    지수 구성종목과 달리 **날짜에 의존하지 않는 종목 마스터**라, 시스템 시계가 미래여서
    pykrx 의 '오늘' 기반 조회가 빈 응답을 주는 환경에서도 동작한다. 종목명 매핑(체결/주문
    로그 표기)의 신뢰 가능한 1차 소스. 미인증/실패 시 빈 리스트(호출부가 폴백).

    성공 결과만 캐시한다(실패를 캐시하면 프로세스 수명 내내 이름이 비므로).
    """
    global _STOCKS_CACHE
    if _STOCKS_CACHE is not None:
        return _STOCKS_CACHE

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — 전종목 목록 조회 건너뜀")
        return []

    payload = {
        "bld": _BLD_STOCK_FINDER, "locale": "ko_KR",
        "mktsel": "ALL", "typeNo": "0", "searchText": "",
    }
    try:
        resp = sess.post(_JSON_URL, data=payload, timeout=20)
        rows = resp.json().get("block1") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("KRX 전종목 목록 조회 실패: %s", e)
        return []

    out: list[dict[str, str]] = []
    for r in rows:
        code = str(r.get("short_code") or "").strip().zfill(6)
        name = str(r.get("codeName") or "").strip()
        if not code or not name:
            continue
        market = _MARKET_BY_CODE.get(str(r.get("marketCode") or "").strip(), "")
        out.append({"code": code, "name": name, "market": market})

    if out:  # 성공 시에만 캐시
        _STOCKS_CACHE = out
        logger.info("KRX 전종목 목록 로드: %d개", len(out))
    return out


def market_caps(as_of: date) -> dict[str, int]:
    """as_of 시점 전 종목 시가총액(원) {code: mktcap}. 실패/미인증 시 빈 dict.

    MDCSTAT01501 을 KOSPI(STK)·KOSDAQ(KSQ) 각각 조회해 합친다. 명시적 trdDd 를 쓰므로
    시스템 시계가 미래여도 과거일 시총을 정상 조회한다. 휴장일이면 최대 6일 소급 스냅.
    유동성 필터(universe_rule.min_market_cap)에서 소형주를 후보풀에서 걸러내는 용도.
    성공 결과만 캐시한다(실패를 캐시하지 않음).
    """
    key = as_of.strftime("%Y%m%d")
    if key in _MKTCAP_CACHE:
        return _MKTCAP_CACHE[key]

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — 시가총액 조회 건너뜀")
        return {}

    caps: dict[str, int] = {}
    for back in range(7):  # 휴장일 빈 응답 대비 직전 영업일 소급
        dd = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        for mkt in ("STK", "KSQ"):
            payload = {
                "bld": _BLD_MARKET_CAP, "locale": "ko_KR",
                "mktId": mkt, "trdDd": dd, "money": "1", "csvxls_isNo": "false",
            }
            try:
                resp = sess.post(_JSON_URL, data=payload, timeout=20)
                rows = resp.json().get("OutBlock_1") or []
            except Exception as e:  # noqa: BLE001
                logger.warning("KRX 시가총액 조회 실패(%s %s): %s", mkt, dd, e)
                rows = []
            for r in rows:
                code = str(r.get("ISU_SRT_CD") or "").strip().zfill(6)
                raw = str(r.get("MKTCAP") or "").replace(",", "").strip()
                if code and raw.isdigit():
                    caps[code] = int(raw)
        if caps:
            break

    if caps:
        _MKTCAP_CACHE[key] = caps
    return caps


def sector_map(as_of: date | None = None) -> dict[str, str]:
    """전 상장종목의 업종명 매핑 {code: 업종명} 을 KRX MDC(MDCSTAT03901)로 조회한다.

    KOSPI(STK)·KOSDAQ(KSQ) 각 시장을 요청 1회씩(총 2회)만 호출해 전종목 업종을 얻는다
    — OpenDART 기업개황(종목당 1회 호출) 대비 압도적으로 효율적이라 이쪽을 채택했다.
    업종 분류는 사실상 정적이므로 (as_of 는 KRX 가 요구하는 조회 기준일일 뿐) 프로세스 내
    1회 로드 후 캐시를 재사용한다. 미인증/실패/무자료 시 빈 dict 를 반환한다(호출부가
    섹터 한도를 미적용으로 폴백하도록). 성공 결과만 캐시한다.

    :param as_of: 조회 기준일(기본 today). 휴장일이면 최대 9일 소급해 직전 영업일로 스냅.
        업종 분류 자체는 시점 의존이 낮으므로 정확한 PIT 는 요구하지 않는다.
    """
    global _SECTOR_CACHE
    if _SECTOR_CACHE is not None:
        return _SECTOR_CACHE

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — 업종분류 조회 건너뜀")
        return {}

    base = as_of or date.today()
    mapping: dict[str, str] = {}
    for back in range(10):  # 휴장일·미래시계 대비 직전 영업일 소급
        dd = (base - timedelta(days=back)).strftime("%Y%m%d")
        for mkt in ("STK", "KSQ"):
            payload = {
                "bld": _BLD_SECTOR, "locale": "ko_KR",
                "mktId": mkt, "trdDd": dd, "money": "1", "csvxls_isNo": "false",
            }
            try:
                resp = sess.post(_JSON_URL, data=payload, timeout=20)
                rows = resp.json().get("block1") or []
            except Exception as e:  # noqa: BLE001
                logger.warning("KRX 업종분류 조회 실패(%s %s): %s", mkt, dd, e)
                rows = []
            for r in rows:
                code = str(r.get("ISU_SRT_CD") or "").strip().zfill(6)
                ind = str(r.get("IDX_IND_NM") or "").strip()
                if code != "000000" and ind:
                    mapping[code] = ind
        if mapping:
            break

    if mapping:
        _SECTOR_CACHE = mapping
        logger.info("KRX 업종분류 로드: %d종목", len(mapping))
    return mapping


def membership_union(dates: list[date], index: str = "KOSPI200") -> dict[str, list[str]]:
    """여러 시점의 구성종목을 한 번에 조회해 {YYYYMMDD: [codes]} 로 반환한다.

    백테스트 전처리용 — 편입 종목의 합집합으로 가격을 선적재할 때 쓴다.
    """
    out: dict[str, list[str]] = {}
    for d in dates:
        out[d.strftime("%Y%m%d")] = index_members(d, index)
    return out
