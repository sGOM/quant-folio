"""KRX 지수 구성종목(시점별 멤버십) 클라이언트.

목적: 백테스트의 생존편향(survivorship bias)을 제거하기 위한 **시점별 지수 구성종목**
공급. "오늘 살아남은 종목"이 아니라 각 과거 시점에 실제로 지수(예: KOSPI200)에
속했던 종목 집합을 반환하므로, 편출·상장폐지된 종목까지 후보풀에 포함해 공정하게
검증할 수 있다.

데이터 소스: KRX 정보데이터시스템(data.krx.co.kr) MDC. 이 포털은 회원 로그인을
요구하며(미인증 요청은 "LOGOUT" 반환), pykrx 1.2.x 가 KRX_ID/KRX_PW 환경변수로
로그인 세션을 관리한다(app.core.config 가 시크릿에서 주입). 여기서는 그 인증
세션을 재사용해 구성종목 JSON(MDCSTAT00601)을 직접 조회한다.

설계 원칙(opendart.py 와 동일, 단 인증 실패 처리는 §44-1 이후 아래처럼 갈린다):
- 블로킹(sync) 함수 — 호출부가 run_in_threadpool 로 실행.
- 미설정(KRX_ID/PW 없음)·무자료(휴장일)는 예외 대신 빈 리스트/딕셔너리 반환(호출부가
  폴백하도록). 반면 실제 로그인 실패·전송 실패는 `app.services.data.errors` 의 원인별
  `DataSourceError` 로 raise 한다(`_session`/`_krx_rows`) — 미설정과 실패를 조용히
  같은 값(None/[])으로 뭉개던 것이 KRX 차단 시 빈 패널 위 백테스트를 낳은 재발 경로였다.
  각 조회 함수의 7일/10일 소급 루프도 **전량 실패**(정상 응답을 한 번도 못 받음)면 그
  예외(대표)를 raise 한다. 정상 응답이 있었는데 전부 비었으면(휴장일·미상장) 빈
  리스트/딕셔너리를 그대로 반환한다 — "성공"은 예외 없이 응답을 받은 것이지 데이터를
  얻은 것이 아니다.
- 시점별 결과를 모듈 캐시(_MEMBERS_CACHE)에 저장(같은 날짜 반복조회 방지).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.services.data.errors import (
    DataSourceError,
    SourceAuthError,
    SourceRequestError,
    SourceSchemaError,
    SourceUnavailableError,
    clear_cooldown,
    cooldown_remaining,
    note_failure,
    representative,
)
from app.services.data.loader import bounded_socket_timeout
from app.services.market import is_business_day

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


def _build_session():
    """pykrx 로그인 1회. 테스트에서 목으로 갈아끼우기 위해 분리한다."""
    from pykrx.website.comm import auth

    # build_krx_session() 은 로그인 로그 이후에도 내부적으로 추가 요청을 할 수 있는데,
    # 그 경로엔 우리가 손댈 수 없는 timeout 미지정 호출이 있을 수 있다(실제로 로그인
    # 완료 로그 이후 응답 없이 멈추는 현상을 관측함). 소켓 레벨로 강제 타임아웃을 건다.
    with bounded_socket_timeout(20):
        return auth.build_krx_session()


def _session():
    """pykrx 인증 KRX 세션을 반환한다.

    **미설정과 실패를 가른다** — 둘 다 None 이던 것이 §44-1 재발 경로였다.
    자격증명이 없으면 애초에 물어보지 않은 것이므로 None(실패 아님). 로그인 실패·
    빈 세션·쿨다운 중은 SourceAuthError 를 던진다.

    쿨다운 검사를 **"이미 유효한 세션이면 재사용" 분기보다 앞에서** 한다. 뒤에 두면
    쿨다운 중에도 캐시된 유효 세션이 있으면 그걸 그대로 돌려줘, 그 세션으로 보내는
    `_krx_rows` POST 가 쿨다운과 무관하게 계속 나간다 — §44-1 이 실제로 겪은 형태
    (세션 쿠키는 살아 있는데 응답만 차단 페이지)를 쿨다운이 못 막는 결함이 된다.
    """
    if not (settings.KRX_ID and settings.KRX_PW):
        return None  # 미설정 — 실패가 아니다(용도별 preflight 가 필요한 곳에서 막는다)

    remaining = cooldown_remaining("krx")
    if remaining > 0:
        raise SourceAuthError("krx", f"로그인 쿨다운 중 — 재시도 생략({remaining:.0f}초 남음)")

    try:
        from pykrx.website.comm import auth
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailableError("krx", f"pykrx 로드 실패: {e}") from e

    sess = getattr(auth, "_auth_session", None)
    if sess is not None and getattr(sess, "is_valid", lambda: False)():
        return sess

    try:
        built = _build_session()
    except Exception as e:  # noqa: BLE001
        exc = SourceAuthError("krx", f"인증 세션 생성 실패: {e}")
        note_failure(exc)
        logger.warning("%s", exc)
        raise exc from e
    if built is None:
        exc = SourceAuthError("krx", "인증 세션 생성 실패(빈 세션)")
        note_failure(exc)
        logger.warning("%s", exc)
        raise exc
    clear_cooldown("krx")
    return built


def has_krx_auth() -> bool:
    """KRX 인증 세션을 확보할 수 있는지. preflight 용 — 예외를 던지지 않는다."""
    try:
        return _session() is not None
    except DataSourceError:
        return False


def require_krx_auth() -> None:
    """KRX 인증이 **필수** 인 실행의 진입점에서 호출한다(PIT 유니버스 백테스트 등).

    미설정 자체는 실패가 아니지만(개발환경에서 앱은 떠야 한다), PIT 유니버스처럼
    없으면 결과가 무의미해지는 필수 입력에는 다르다. 인증 없이 돌리면 모든 조회가
    빈 값을 주고 백테스트가 **빈 패널 위에서 '성공'** 한다(§44-1 과 동일한 결과).
    필수/선택을 소스가 아니라 용도로 가르기 위한 사전 검사.

    실패 시 **원인을 갈라서** 올린다. "없거나 실패했다"로 뭉치면 운영자가 시크릿을
    확인해야 하는지 KRX 차단이 풀리기를 기다려야 하는지 알 수 없다. 세 갈래 모두
    메모리 읽기(설정값·쿨다운 레지스트리)라 여기서 추가 로그인 시도는 일어나지 않는다
    — `has_krx_auth()` 를 유일한 관문으로 두는 구조도 그대로다(테스트 격리 지점).
    """
    if has_krx_auth():
        return
    if not (settings.KRX_ID and settings.KRX_PW):
        raise SourceAuthError(
            "krx", "KRX_ID/KRX_PW 가 설정되지 않았다 — PIT 유니버스 백테스트에는 필수다"
        )
    remaining = cooldown_remaining("krx")
    if remaining > 0:
        raise SourceAuthError(
            "krx", f"KRX 로그인 실패 후 쿨다운 중({remaining:.0f}초 남음)"
        )
    raise SourceAuthError(
        "krx", "KRX 로그인에 실패했다 — 자격증명 유효성·차단 여부를 확인하라"
    )


def _krx_rows(sess, payload: dict, key: str, label: str, timeout: float = 20) -> list[dict]:
    """KRX MDC POST 1회. 실패는 원인별 DataSourceError 로 raise 한다.

    KRX 는 에러 코드 체계가 없다. §44-1 에서 관측한 차단 동작(**HTTP 200 인데 본문이
    JSON 이 아닌 HTML 안내 페이지**)을 인증 실패로 판별한다. 이 휴리스틱이 빗나가면
    Unavailable 로 분류돼 60초 쿨다운이 걸린다 — 오분류여도 재시도 폭주는 막힌다.

    정상 JSON 이면서 리스트가 비어 있는 것은 **실패가 아니다**(휴장일·미상장).
    호출자가 직전 영업일로 소급하도록 빈 리스트를 그대로 돌려준다.

    쿨다운을 **거는 것**은 여기서만 한다 — 호출자(단일 조회/7일 루프)마다 거는 위치가
    달라지면 어떤 경로는 쿨다운 없이 폭주한다. 반대로 쿨다운을 **검사**하는 것도 맨 앞에서
    한다 — §44-1 의 실제 형태(세션 쿠키는 살아 있고 응답만 차단 페이지)에서는 `_session()`
    의 검사만으로 부족하다. `_session()` 을 거치지 않고 이미 확보한 `sess` 로 반복 호출하는
    7일/9일 소급 루프가 쿨다운 중에도 매번 POST 를 내보내기 때문이다. 여기서 막으면 그
    루프의 나머지 반복도 함께 막힌다. 이미 걸린 쿨다운을 검사만 하고 갱신하지는 않는다
    (여기서 매번 note_failure 하면 호출이 계속 들어오는 한 쿨다운이 끝없이 연장된다).
    """
    remaining = cooldown_remaining("krx")
    if remaining > 0:
        raise SourceAuthError(
            "krx", f"{label} 조회 생략 — 쿨다운 중({remaining:.0f}초 남음)"
        )

    def _fail(exc: DataSourceError) -> DataSourceError:
        note_failure(exc)
        return exc

    try:
        resp = sess.post(_JSON_URL, data=payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise _fail(SourceUnavailableError("krx", f"{label} 연결 실패: {e}")) from e

    status = getattr(resp, "status_code", 200)
    if status >= 500:
        raise _fail(SourceUnavailableError("krx", f"{label} 서버 오류: HTTP {status}"))
    if status >= 400:
        raise _fail(SourceRequestError("krx", f"{label} 요청 오류: HTTP {status}"))

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise _fail(
            SourceAuthError("krx", f"{label} 응답이 JSON 이 아니다(차단 추정): {e}")
        ) from e

    if not isinstance(data, dict) or key not in data:
        # Schema 는 cooldown=None 이라 _fail 을 거쳐도 쿨다운이 걸리지 않는다(의도).
        raise _fail(
            SourceSchemaError("krx", f"{label} 응답에 '{key}' 키가 없다: {str(data)[:120]}")
        )
    rows = data.get(key) or []
    # 키가 있어도 값의 **형태**까지 맞는지 여기서 확정한다. 호출자들은 반환 직후
    # `r.get(...)` 으로 파싱하는데 그 구간은 이 try 밖이라, 원소가 dict 가 아니면
    # AttributeError 가 DataSourceError 계약을 뚫고 그대로 샌다. 호출자는 좁힌
    # `except DataSourceError` 로 저하하므로, 그런 응답은 저하되지 못하고 곧장
    # 크래시한다(웹은 500, 라이브 엔진은 틱 실패). 상류 형식 문제는 Schema 로 귀속한다.
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise _fail(
            SourceSchemaError(
                "krx", f"{label} 응답의 '{key}' 가 dict 리스트가 아니다: {str(rows)[:120]}"
            )
        )
    return rows


def index_members(as_of: date, index: str = "KOSPI200") -> list[str]:
    """as_of 시점에 index 를 구성하던 종목코드(6자리) 목록.

    조회 순서: (1) 인메모리 캐시(_MEMBERS_CACHE, 프로세스 수명), (2) 로컬 영구 저장소
    (원장이 해당 (index, as_of) 조합을 final=True 로 확정 기록했으면 KRX 세션조차
    만들지 않고 그 저장분을 반환 — §44-1 차단 시에도 백테스트가 살아남는다), (3) 그래도
    없으면 KRX MDC 를 직접 조회한다.

    전량 실패 시 DataSourceError. **원장에 확정 기록이 없는 한** 미인증 시 빈 값을
    반환한다(원장에 final=True 로 남아 있으면 미인증이어도 (2)에서 저장분을 반환한다).
    7일 소급 루프 중 정상 응답을 한 번도 받지 못했으면(전량 실패) 대표 예외를 raise 하고,
    정상 응답이 있었는데 전부 빈 리스트였으면(휴장일·미상장) 빈 리스트를 그대로
    반환한다 — 단, 이 **빈 결과는 원장·로컬 저장소에 확정 기록하지 않는다**(codes 가
    있을 때만 기록). 빈 결과를 굳히면 다음 호출부터 KRX 세션조차 만들지 않고 영구히
    []를 반환하게 되기 때문이다(수동 DB 삭제 전까지 회복 불가 — §47). `_session()`
    자체가 던지는 `SourceAuthError`(로그인 실패·쿨다운 중)는 잡지 않고 호출자에게 그대로
    전파한다 — 호출자 정리는 별도 작업.

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

    # 2차 캐시: 로컬 영구 저장소. PIT 지수구성은 확정 후 불변이므로 한 번 적재되면
    # KRX 로그인 없이도 조회된다(§44-1 차단 시 백테스트가 살아남는다).
    from app.services.data.store import indexes as _indexes
    from app.services.data.store.frame import is_final_date, make_cache_key
    from app.services.data.store.ledger import default_ledger

    _store_key = make_cache_key(index, as_of)
    _entry = default_ledger().get("index_members", _store_key)
    if _entry is not None and _entry.final:
        codes = _indexes.read_constituents(index, as_of)
        _MEMBERS_CACHE[key] = codes
        return codes

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — %s 구성종목 조회 건너뜀", index)
        return []

    codes: list[str] = []
    errors: list[DataSourceError] = []
    ok = False  # '성공' = 예외 없이 응답을 받음(데이터 획득이 아니다)
    # 휴장일 빈 응답 대비 최대 6일(주말+연휴) 직전 영업일까지 스냅.
    for back in range(7):
        # 직전 반복이 쿨다운을 걸었으면 더 시도해도 _krx_rows 가 즉시 같은 차단을
        # 재현할 뿐이다 — 계속 돌리면 대표 원인이 이 자기유발 차단(Auth)으로
        # 오염된다(원래 원인이 Unavailable 이어도). 쿨다운 검사 자체는 _krx_rows 의
        # 몫이므로 여기서는 "더 부르지 않는다"만 판단한다.
        if errors and cooldown_remaining("krx") > 0:
            break
        dd = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        payload = {
            "bld": _BLD_INDEX_CONSTITUENTS, "locale": "ko_KR",
            "trdDd": dd, "money": "1", "csvxls_isNo": "false", **params,
        }
        try:
            rows = _krx_rows(sess, payload, "output", f"{index} 구성종목", timeout=15)
        except DataSourceError as e:
            logger.warning("KRX %s 구성종목 조회 실패(%s): %s", index, dd, e)
            errors.append(e)
            continue
        ok = True
        codes = [str(r.get("ISU_SRT_CD")).zfill(6) for r in rows if r.get("ISU_SRT_CD")]
        if codes:
            break

    # 정상 응답이 한 번도 없었으면 실패다. 정상 응답이 있었는데 전부 비었으면
    # 진짜 휴장/미상장이므로 빈 값을 그대로 돌려준다.
    # 쿨다운은 _krx_rows 가 이미 걸었다 — 여기서 또 걸지 않는다.
    if not ok and errors:
        raise representative(errors)

    # codes 가 있을 때만 로컬 스토어(원장 + PIT 지수구성)에 확정 기록한다 — 아래
    # 인메모리 캐시와 동일한 원칙이다. 정상 JSON 이면서 codes 만 빈 경우(예: KRX 응답
    # 스키마가 미세하게 바뀌어 `ISU_SRT_CD` 키가 사라짐)는 `_krx_rows` 가 "dict 리스트"
    # 까지만 검증하므로 예외 없이 통과한다(ok=True). 이 빈 결과를 원장에 final=True 로
    # 굳히면, 이후 모든 호출이 259행의 2차 캐시에서 `final==True` 를 보고 **KRX 세션조차
    # 만들지 않고** [] 를 반환한다 — 인메모리 캐시와 달리 프로세스 재시작으로도 회복되지
    # 않고 수동 DB 삭제 전까지 영구히 0종목으로 고착된다(§47 재발 형태).
    # 트레이드오프: 지수 개설 이전 날짜 등 "진짜 0종목"인 시점은 매 호출마다 재조회하게
    # 되지만(KRX 세션 확보 비용), 잘못 굳혀 영구 0종목이 되는 쪽이 비교할 수 없이 위험하다.
    if codes:
        _indexes.write_constituents(index, as_of, codes)
        default_ledger().put(
            "index_members", _store_key, row_count=len(codes), final=is_final_date(as_of)
        )

    # 성공 결과만 캐시한다(실패/미인증/일시 장애의 빈 응답을 캐시하면 프로세스 수명
    # 내내 해당 시점 구성이 []로 고착 → 조용히 고정 유니버스로 폴백하는 생존편향 재유입).
    if codes:
        _MEMBERS_CACHE[key] = codes
    return codes


def all_listed_stocks() -> list[dict[str, str]]:
    """전 상장종목 목록 [{code, name, market}] 을 KRX MDC finder 로 조회한다.

    지수 구성종목과 달리 **날짜에 의존하지 않는 종목 마스터**라, 시스템 시계가 미래여서
    pykrx 의 '오늘' 기반 조회가 빈 응답을 주는 환경에서도 동작한다. 종목명 매핑(체결/주문
    로그 표기)의 신뢰 가능한 1차 소스.

    전량 실패 시 DataSourceError. 미인증 시 빈 값. 루프 없는 단일 조회라 POST 실패는
    그대로 전파한다. `_session()` 이 던지는 `SourceAuthError`(로그인 실패·쿨다운 중)도
    잡지 않고 그대로 전파한다 — 호출자 정리는 별도 작업.

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
    # 루프가 없는 단일 조회다 — _krx_rows 의 예외를 그대로 전파한다(쿨다운은
    # _krx_rows 가 이미 걸었다).
    rows = _krx_rows(sess, payload, "block1", "전종목목록")

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
    """as_of 시점 전 종목 시가총액(원) {code: mktcap}.

    MDCSTAT01501 을 KOSPI(STK)·KOSDAQ(KSQ) 각각 조회해 합친다. 명시적 trdDd 를 쓰므로
    시스템 시계가 미래여도 과거일 시총을 정상 조회한다. 휴장일이면 최대 6일 소급 스냅.
    유동성 필터(universe_rule.min_market_cap)에서 소형주를 후보풀에서 걸러내는 용도.

    전량 실패 시 DataSourceError. 미인증 시 빈 값. 바깥 날짜 루프×안쪽 시장(STK/KSQ)
    루프 중 정상 응답을 한 번도 받지 못했으면 대표 예외를 raise 한다. `_session()` 이
    던지는 `SourceAuthError`(로그인 실패·쿨다운 중)는 잡지 않고 그대로 전파한다 — 호출자
    정리는 별도 작업. 성공 결과만 캐시한다(실패를 캐시하지 않음).
    """
    key = as_of.strftime("%Y%m%d")
    if key in _MKTCAP_CACHE:
        return _MKTCAP_CACHE[key]

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — 시가총액 조회 건너뜀")
        return {}

    caps: dict[str, int] = {}
    errors: list[DataSourceError] = []
    ok = False
    for back in range(7):  # 휴장일 빈 응답 대비 직전 영업일 소급
        dd = (as_of - timedelta(days=back)).strftime("%Y%m%d")
        for mkt in ("STK", "KSQ"):
            # 직전 반복이 쿨다운을 걸었으면 더 시도해도 자기유발 차단(Auth)만
            # 반복되며 대표 원인을 오염시킨다 — 더 부르지 않는다.
            if errors and cooldown_remaining("krx") > 0:
                break
            payload = {
                "bld": _BLD_MARKET_CAP, "locale": "ko_KR",
                "mktId": mkt, "trdDd": dd, "money": "1", "csvxls_isNo": "false",
            }
            try:
                rows = _krx_rows(sess, payload, "OutBlock_1", f"시가총액({mkt})")
            except DataSourceError as e:
                logger.warning("KRX 시가총액 조회 실패(%s %s): %s", mkt, dd, e)
                errors.append(e)
                continue
            ok = True
            for r in rows:
                code = str(r.get("ISU_SRT_CD") or "").strip().zfill(6)
                raw = str(r.get("MKTCAP") or "").replace(",", "").strip()
                if code and raw.isdigit():
                    caps[code] = int(raw)
        if errors and cooldown_remaining("krx") > 0:
            break
        if caps:
            break

    # 쿨다운은 _krx_rows 가 이미 걸었다 — 여기서 또 걸지 않는다.
    if not ok and errors:
        raise representative(errors)

    if caps:
        _MKTCAP_CACHE[key] = caps
    return caps


def sector_map(as_of: date | None = None) -> dict[str, str]:
    """전 상장종목의 업종명 매핑 {code: 업종명} 을 반환한다.

    KOSPI(STK)·KOSDAQ(KSQ) 각 시장을 요청 1회씩(총 2회)만 호출해 전종목 업종을 얻는다
    — OpenDART 기업개황(종목당 1회 호출) 대비 압도적으로 효율적이라 이쪽을 채택했다.
    업종 분류는 사실상 정적이므로 프로세스 내 1회 로드 후 캐시를 재사용한다. 전량 실패 시
    DataSourceError. 미인증 시 빈 값(호출부가 섹터 한도를 미적용으로 폴백하도록). 무자료
    (휴장일)면 빈 dict 를 그대로 반환한다. `_session()` 이 던지는 `SourceAuthError`(로그인
    실패·쿨다운 중)는 잡지 않고 그대로 전파한다 — 호출자 정리는 별도 작업. 성공 결과만
    캐시한다.

    :param as_of: PIT 조회 시점. 주어지면 먼저 ``sector_map_snapshots`` 테이블에서 as_of
        이전(포함) 가장 가까운 스냅샷(``snapshot_sector_map`` 이 분기 1회 적재, worker
        beat "snapshot-sector-map-quarterly")을 조회해 그 시점 분류를 PIT 로 반환한다.
        스냅샷이 하나도 없으면(스냅샷 도입 이전 구간이거나 아직 첫 배치 전) 아래의 '현재
        KRX 분류' 조회로 폴백하고 그 사실을 warning 로그로 남긴다. None 이면 스냅샷 조회를
        건너뛰고 곧바로 현재 분류를 반환한다(과거 동작과 동일 — KRX 가 요구하는 조회
        기준일로만 쓰이며, 휴장일이면 최대 9일 소급해 직전 영업일로 스냅한다).

    한계(C-2, 부분 해소): KRX MDC 자체는 여전히 '현재' 분류만 제공하고 과거 임의 시점의
    분류를 조회하는 API 가 없다. 이를 우회하기 위해 스냅샷을 지금부터 주기 적재하기
    시작했다(분기 1회로 충분 — 업종 재편 빈도가 낮음) — **스냅샷 도입 이후** 시점부터는
    이 함수가 실제 PIT 매핑을 준다. 스냅샷 도입 **이전** 과거 구간(수년치 백테스트의 초기
    구간 등)은 그 시점의 분류를 보존한 데이터 소스가 없어 소급 적용이 여전히 불가능하며,
    이 경우 현재 분류로 폴백한다(약한 look-ahead 잔존 — 호출부: portfolio.py 의 섹터 캡
    로딩 참고). 섹터 '한도'(비중 상한) 용도라 왜곡은 제한적이다.
    """
    if as_of is not None:
        snap = _lookup_pit_snapshot_sync(as_of)
        if snap:
            return snap
        logger.warning(
            "업종분류 PIT 스냅샷 미확보(%s 이전 스냅샷 없음) — 현재 KRX 분류로 폴백"
            "(C-2 잔존: 스냅샷 도입 이전 구간은 소급 불가).", as_of,
        )

    global _SECTOR_CACHE
    if _SECTOR_CACHE is not None:
        return _SECTOR_CACHE

    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — 업종분류 조회 건너뜀")
        return {}

    base = as_of or date.today()
    mapping: dict[str, str] = {}
    errors: list[DataSourceError] = []
    ok = False
    for back in range(10):  # 휴장일·미래시계 대비 직전 영업일 소급
        dd = (base - timedelta(days=back)).strftime("%Y%m%d")
        for mkt in ("STK", "KSQ"):
            # 직전 반복이 쿨다운을 걸었으면 더 시도해도 자기유발 차단(Auth)만
            # 반복되며 대표 원인을 오염시킨다 — 더 부르지 않는다.
            if errors and cooldown_remaining("krx") > 0:
                break
            payload = {
                "bld": _BLD_SECTOR, "locale": "ko_KR",
                "mktId": mkt, "trdDd": dd, "money": "1", "csvxls_isNo": "false",
            }
            try:
                rows = _krx_rows(sess, payload, "block1", f"업종분류({mkt})")
            except DataSourceError as e:
                logger.warning("KRX 업종분류 조회 실패(%s %s): %s", mkt, dd, e)
                errors.append(e)
                continue
            ok = True
            for r in rows:
                code = str(r.get("ISU_SRT_CD") or "").strip().zfill(6)
                ind = str(r.get("IDX_IND_NM") or "").strip()
                if code != "000000" and ind:
                    mapping[code] = ind
        if errors and cooldown_remaining("krx") > 0:
            break
        if mapping:
            break

    # 쿨다운은 _krx_rows 가 이미 걸었다 — 여기서 또 걸지 않는다.
    if not ok and errors:
        raise representative(errors)

    if mapping:
        _SECTOR_CACHE = mapping
        logger.info("KRX 업종분류 로드: %d종목", len(mapping))
    return mapping


def _lookup_pit_snapshot_sync(as_of: date) -> dict[str, str]:
    """as_of 이전(포함) 가장 최근 업종분류 스냅샷 {code: 업종명} 을 동기 컨텍스트에서 조회한다.

    이 모듈의 다른 함수처럼 블로킹 호출부(라우트가 run_in_threadpool 로 돌리는 스레드)
    에서 쓰이므로, 이벤트루프가 없는 스레드에서 asyncio.run 으로 비동기 DB 세션을 돌린다
    (worker.tasks 의 asyncio.run(...) 과 동일 패턴). 이미 실행 중인 이벤트루프 안(예:
    향후 async 호출부)에서 부르면 asyncio.run 이 RuntimeError 를 내므로, 그런 호출부는
    _lookup_pit_snapshot 코루틴을 직접 await 할 것. DB 미가동·미마이그레이션·네트워크
    오류 등은 모두 여기서 흡수해 빈 dict 를 반환한다(호출부 sector_map 이 현재 분류로
    자동 폴백하도록 — 자가복구).
    """
    try:
        return asyncio.run(_lookup_pit_snapshot(as_of))
    except Exception as e:  # noqa: BLE001
        logger.warning("업종분류 PIT 스냅샷 조회 실패(%s): %s", as_of, e)
        return {}


async def _lookup_pit_snapshot(as_of: date) -> dict[str, str]:
    """as_of 이전(포함) 가장 최근 스냅샷의 {code: 업종명} 을 비동기로 조회한다.

    스냅샷은 배치(snapshot_sector_map)가 한 번에 전종목을 같은 snapshot_date 로 적재하므로,
    '가장 최근 snapshot_date' 하나만 찾으면 그 날짜의 전 종목 매핑을 그대로 쓸 수 있다.
    """
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from app.models import SectorMapSnapshot

    async with AsyncSessionLocal() as db:
        latest_date = await db.scalar(
            select(sa_func.max(SectorMapSnapshot.snapshot_date)).where(
                SectorMapSnapshot.snapshot_date <= as_of
            )
        )
        if latest_date is None:
            return {}
        rows = await db.execute(
            select(SectorMapSnapshot.symbol, SectorMapSnapshot.sector).where(
                SectorMapSnapshot.snapshot_date == latest_date
            )
        )
        mapping = {code: sector for code, sector in rows.all()}
        if mapping:
            logger.info(
                "업종분류 PIT 스냅샷 사용: snapshot_date=%s(요청 as_of=%s) %d종목",
                latest_date, as_of, len(mapping),
            )
        return mapping


async def snapshot_sector_map(
    db, as_of: date | None = None, *, force: bool = False,
) -> int:
    """현재 KRX 업종분류를 ``sector_map_snapshots`` 에 적재한다(C-2 해소용 주기 배치).

    분기 1회(worker beat "snapshot-sector-map-quarterly", worker.snapshot_sector_map
    태스크) 실행을 전제로 한다 — 업종 재편 빈도가 낮아 더 촘촘한 주기는 불필요하다.

    :param db: 비동기 DB 세션(AsyncSession). 이 함수는 flush 만 하고 commit 은 호출부
        책임이다(다른 배치 함수들과 동일 패턴).
    :param as_of: 스냅샷 날짜(기본 오늘). 이미 이 날짜의 스냅샷이 있으면 force=True 가
        아닌 한 건너뛴다 — 같은 배치를 중복 실행해도 멱등이 되도록.
    :param force: True 면 기존 as_of 스냅샷을 삭제 후 재적재한다.
    :return: 적재한 종목 수. 이미 적재됨(멱등 스킵) 또는 ``sector_map()`` 이 미인증·
        휴장일 등으로 빈 매핑을 반환한 경우에만 0.
    :raises DataSourceError: ``sector_map()`` 이 전량 실패로 raise 하면 그대로 전파한다
        (Task 3 이후 ``sector_map()`` 은 부분 실패를 삼키지 않는다).
    """
    from sqlalchemy import delete, func as sa_func, select

    from app.models import SectorMapSnapshot

    snap_date = as_of or date.today()

    existing = await db.scalar(
        select(sa_func.count()).select_from(SectorMapSnapshot).where(
            SectorMapSnapshot.snapshot_date == snap_date
        )
    )
    if existing and not force:
        logger.info(
            "업종분류 스냅샷(%s) 이미 존재(%d건) — 스킵(force=True 로 재적재 가능)",
            snap_date, existing,
        )
        return 0

    mapping = sector_map()  # 현재 KRX 분류(모듈 캐시 재사용, 없으면 신규 조회)
    # 전량 실패는 sector_map() 이 DataSourceError 로 raise 해 여기까지 오지 않는다.
    # 도달하는 빈 매핑은 미인증(preflight 미통과) 또는 휴장일 빈 응답뿐이다.
    if not mapping:
        logger.warning("업종분류 미인증/빈 응답 — 스냅샷(%s) 적재 건너뜀", snap_date)
        return 0

    if existing:
        await db.execute(
            delete(SectorMapSnapshot).where(SectorMapSnapshot.snapshot_date == snap_date)
        )

    db.add_all(
        [
            SectorMapSnapshot(symbol=code, sector=sector, snapshot_date=snap_date)
            for code, sector in mapping.items()
        ]
    )
    await db.flush()
    logger.info("업종분류 스냅샷 적재: snapshot_date=%s %d종목", snap_date, len(mapping))
    return len(mapping)


def membership_union(dates: list[date], index: str = "KOSPI200") -> dict[str, list[str]]:
    """여러 시점의 구성종목을 한 번에 조회해 {YYYYMMDD: [codes]} 로 반환한다.

    백테스트 전처리용 — 편입 종목의 합집합으로 가격을 선적재할 때 쓴다.

    :raises DataSourceError: 내부에서 호출하는 ``index_members()`` 가 특정 날짜에
        전량 실패하면 그대로 전파한다(부분 실패를 삼키지 않는다).
    """
    out: dict[str, list[str]] = {}
    for d in dates:
        out[d.strftime("%Y%m%d")] = index_members(d, index)
    return out


# ─────────────────────────── 레버리지 ETF 잔고 ───────────────────────────
#
# 지수 구성종목과 소스(같은 MDC 포털·같은 인증 세션)가 같아 이 모듈에 둔다.
# 용도는 다르다 — 이쪽은 생존편향 제거가 아니라 시장 **취약성**(레버리지 축적) 관측이다.
# 2026-07 폭락의 동력이 단일종목 레버리지 ETF 의 강제 리밸런싱 나선이었고, 그 축적은
# 가격이 오르는 동안 관측된다(사전 지표). 미수금·신용융자 쪽은 app/services/data/kofia.py.

_BLD_ETF_ALL = "dbms/MDC/STAT/standard/MDCSTAT04301"

# 종목명으로 레버리지 여부를 판별한다. KRX 응답에 배수·기초자산 구분 필드가 없어
# 종목명이 유일한 단서다. '인버스'는 하락 베팅이라 상승장 취약성 축적과 성격이 달라
# 레버리지 집계에서 제외한다(인버스2X 는 레버리지이기도 하지만 방향이 반대).
_LEV_KEYWORDS = ("레버리지",)
# 단일종목 레버리지 — 2026-07 나선의 진앙. 개별 종목 변동성이 그대로 증폭돼
# 지수형 레버리지보다 청산 압력이 크다.
_SINGLE_STOCK_KEYWORD = "단일종목"


def _krx_num(v: object) -> float:
    """KRX 응답의 콤마 포함 숫자 문자열을 float 로(파싱 실패 시 0.0)."""
    try:
        return float(str(v or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def etf_leverage_exposure(as_of: date) -> dict:
    """as_of 시점 레버리지 ETF 잔고 집계.

    전량 실패 시 DataSourceError. 미인증 시 빈 값. 무자료(휴장일)면 빈 dict 를 그대로
    반환한다. `_session()` 이 던지는 `SourceAuthError`(로그인 실패·쿨다운 중)는 잡지
    않고 그대로 전파한다 — 호출자 정리는 별도 작업.

    반환: `{as_of, total_mktcap, leveraged_mktcap, single_stock_mktcap,
    leveraged_ratio, single_stock_ratio, leveraged_count, single_stock_count}`
    (금액 단위: 원, 비율은 0~1).

    **`MKTCAP`(시가총액)을 쓴다.** 같은 응답의 `INVSTASST_NETASST_TOTAMT`(순자산총액)은
    2026-07-31 기준 1155 종목 전부 0 이라 사용할 수 없다. ETF 는 NAV 괴리가 크지 않아
    시가총액이 순자산의 실용적 대용이며, 어차피 쓰임새가 **비율의 시계열 변화**라
    수준 자체의 정확도보다 일관성이 중요하다.

    **휴장일은 조회 전에 스냅한다.** 이 엔드포인트는 휴장일에 빈 응답이 아니라 직전
    영업일 데이터를 그대로 돌려준다(2026-08-01 토요일 요청에서 확인). 응답이 비기를
    기다리는 소급 방식으로는 걸러지지 않아, 반환 `as_of` 가 실제 데이터 시점과 어긋난
    거짓 라벨이 된다 — 시계열로 쌓으면 같은 값이 두 날짜에 중복 기록된다.
    남은 한계: `is_business_day` 가 놓치는 공휴일에서는 여전히 어긋날 수 있다.

    PIT 안전 — 스냅·소급 모두 과거로만 가고 미래일로는 가지 않는다.
    """
    sess = _session()
    if sess is None:
        logger.debug("KRX 미인증 — ETF 레버리지 잔고 조회 건너뜀")
        return {}

    # 주말·공휴일이면 직전 영업일로 스냅(조회 '전'에 — 위 docstring 참고).
    snapped = as_of
    for _ in range(10):
        if is_business_day(snapped):
            break
        snapped -= timedelta(days=1)

    rows: list[dict] = []
    errors: list[DataSourceError] = []
    ok = False
    used = snapped
    for back in range(7):
        # 직전 반복이 쿨다운을 걸었으면 더 시도해도 자기유발 차단(Auth)만
        # 반복되며 대표 원인을 오염시킨다 — 더 부르지 않는다.
        if errors and cooldown_remaining("krx") > 0:
            break
        used = snapped - timedelta(days=back)
        payload = {
            "bld": _BLD_ETF_ALL, "locale": "ko_KR",
            "trdDd": used.strftime("%Y%m%d"),
            "share": "1", "money": "1", "csvxls_isNo": "false",
        }
        try:
            rows = _krx_rows(sess, payload, "output", "ETF 잔고")
        except DataSourceError as e:
            logger.warning("KRX ETF 조회 실패(%s): %s", used, e)
            errors.append(e)
            continue
        ok = True
        if rows:
            break

    # 쿨다운은 _krx_rows 가 이미 걸었다 — 여기서 또 걸지 않는다.
    if not ok and errors:
        raise representative(errors)

    if not rows:
        return {}

    total = lev = single = 0.0
    lev_n = single_n = 0
    for r in rows:
        name = str(r.get("ISU_ABBRV") or "")
        cap = _krx_num(r.get("MKTCAP"))
        total += cap
        if not any(k in name for k in _LEV_KEYWORDS):
            continue
        lev += cap
        lev_n += 1
        if _SINGLE_STOCK_KEYWORD in name:
            single += cap
            single_n += 1

    return {
        "as_of": used,
        "total_mktcap": total,
        "leveraged_mktcap": lev,
        "single_stock_mktcap": single,
        "leveraged_ratio": (lev / total) if total else None,
        "single_stock_ratio": (single / total) if total else None,
        "leveraged_count": lev_n,
        "single_stock_count": single_n,
    }
