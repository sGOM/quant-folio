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

logger = logging.getLogger(__name__)

_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_BLD_INDEX_CONSTITUENTS = "dbms/MDC/STAT/standard/MDCSTAT00601"

# 주요 지수의 MDC 파라미터(indIdx/indIdx2). KOSPI200 = 1/028.
INDEX_PARAMS: dict[str, dict[str, str]] = {
    "KOSPI200": {"indIdx": "1", "indIdx2": "028"},
    "KOSPI100": {"indIdx": "1", "indIdx2": "034"},
    "KRX300": {"indIdx": "5", "indIdx2": "300"},
}

# (index, YYYYMMDD) -> list[str]
_MEMBERS_CACHE: dict[tuple[str, str], list[str]] = {}


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

    _MEMBERS_CACHE[key] = codes
    return codes


def membership_union(dates: list[date], index: str = "KOSPI200") -> dict[str, list[str]]:
    """여러 시점의 구성종목을 한 번에 조회해 {YYYYMMDD: [codes]} 로 반환한다.

    백테스트 전처리용 — 편입 종목의 합집합으로 가격을 선적재할 때 쓴다.
    """
    out: dict[str, list[str]] = {}
    for d in dates:
        out[d.strftime("%Y%m%d")] = index_members(d, index)
    return out
