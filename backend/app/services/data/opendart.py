"""OpenDART(금융감독원 전자공시) 재무 데이터 클라이언트 — 준비/스캐폴딩.

목적: pykrx가 주는 PER/PBR/DIV 를 보완해, 재무제표 기반 지표
(ROE·부채비율·영업이익/순이익 시계열·FCF 등)를 공급하기 위한 소스. 이 지표들은
"우량 가치주"·"실적 상향"·"소형주 턴어라운드" 전략 구현에 필요하지만 현재 데이터
계층에 없다(관련 배경·배선 계획: docs/opendart-integration.md).

현재 상태: **클라이언트 구현 완료(corp_code 매핑·재무제표 조회·파생지표 계산),
전략/백테스트 배선은 진행 중.** API 키가 비어 있으면 모든 조회가 `None`/빈 값을
반환한다(graceful degradation). 키가 secrets/opendart_api_key.txt
(OPENDART_API_KEY_FILE) 또는 OPENDART_API_KEY 로 주입되는 즉시 활성화된다.
파생지표(`derive_metrics`)는 account_id(IFRS 표준 태그) 기반으로 구현·검증했다
(삼성전자·현대차·NAVER 실측 수치 일치).

설계 원칙:
- 새 외부 의존성 없음 — httpx(기존 의존) + 표준 라이브러리(zipfile/xml)만 사용.
- pykrx `_fetch_fundamentals` 와 같은 데이터 계층 규약: 블로킹(sync) 함수이므로
  호출자는 스레드풀(run_in_threadpool)에서 실행한다.
- 실패/키부재/무자료 시 예외 대신 None 반환(호출부가 중립 처리하도록).

PIT(룩어헤드 방지) 주의: 재무 지표를 백테스트에 쓸 때는 반드시 접수번호(rcept_no)
의 공시일(접수일자) 이후 시점부터만 반영해야 한다. 이 클라이언트는 원자료(접수일자
포함)를 그대로 노출하고, 공시일 지연 적용은 배선 단계에서 호출부가 책임진다.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# 보고서 코드(reprt_code): 1분기·반기·3분기·사업보고서(연간).
REPORT_Q1 = "11013"
REPORT_HALF = "11012"
REPORT_Q3 = "11014"
REPORT_ANNUAL = "11011"

# 재무제표 구분(fs_div): 연결(CFS) / 개별(OFS).
FS_CONSOLIDATED = "CFS"
FS_SEPARATE = "OFS"

# OpenDART 정상 응답 status. "013"=조회된 데이터 없음(정상적 무자료).
_STATUS_OK = "000"
_STATUS_NO_DATA = "013"

_TIMEOUT = 15.0


def _mask_key(msg: object) -> str:
    """예외/로그 문자열에서 crtfc_key(API 키) 노출을 차단한다.

    httpx 예외 메시지에는 요청 URL 전체가 담길 수 있어 쿼리의 crtfc_key 값이
    그대로 로그에 남을 위험이 있다. 키 값을 ***로 치환해 반환한다.
    """
    text = str(msg)
    key = settings.OPENDART_API_KEY
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(crtfc_key=)[^&\s]+", r"\1***", text)


def is_enabled() -> bool:
    """OpenDART 조회 가능 여부(API 키 주입 여부). 미승인 단계에서는 False."""
    return settings.has_opendart


def _get(path: str, params: dict) -> dict | None:
    """OpenDART REST 호출 공통부. 키부재/에러/무자료 시 None 반환.

    :param path: 엔드포인트 경로(예: "fnlttSinglAcnt.json")
    :param params: crtfc_key 를 제외한 쿼리 파라미터
    :return: status=="000" 인 응답 dict, 그 외에는 None
    """
    if not is_enabled():
        logger.debug("OpenDART 미활성(API 키 없음) — %s 조회 건너뜀", path)
        return None
    url = f"{settings.OPENDART_BASE_URL}/{path}"
    q = {"crtfc_key": settings.OPENDART_API_KEY, **params}
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params=q)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenDART 호출 실패(%s): %s", path, _mask_key(e))
        return None

    status = str(data.get("status"))
    if status == _STATUS_OK:
        return data
    if status == _STATUS_NO_DATA:
        return None
    logger.warning("OpenDART 응답 오류(%s): status=%s msg=%s", path, status, data.get("message"))
    return None


def corp_code_map() -> dict[str, str] | None:
    """상장사 종목코드(6자리) → OpenDART 고유번호(corp_code, 8자리) 매핑.

    모든 재무제표 API 는 종목코드가 아니라 corp_code 를 요구하므로 선행 조회가
    필요하다. corpCode.xml(zip) 을 내려받아 파싱한다(비교적 크므로 호출부가
    결과를 캐시할 것). 키부재/실패 시 None.
    """
    if not is_enabled():
        return None
    url = f"{settings.OPENDART_BASE_URL}/corpCode.xml"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params={"crtfc_key": settings.OPENDART_API_KEY})
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenDART corpCode 다운로드 실패: %s", _mask_key(e))
        return None

    import xml.etree.ElementTree as ET

    mapping: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_bytes)
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            corp = (item.findtext("corp_code") or "").strip()
            # 상장사만(종목코드 존재). 상폐/비상장은 stock_code 가 공백.
            if stock and corp:
                mapping[stock.zfill(6)] = corp
    except ET.ParseError as e:
        logger.warning("OpenDART corpCode 파싱 실패: %s", e)
        return None
    logger.info("OpenDART corpCode 매핑 로드: %d개 상장사", len(mapping))
    return mapping


def single_company_accounts(
    corp_code: str,
    bsns_year: int,
    reprt_code: str = REPORT_ANNUAL,
    fs_div: str = FS_CONSOLIDATED,
) -> list[dict] | None:
    """단일회사 전체 재무제표 원계정(fnlttSinglAcntAll)을 반환한다.

    반환은 OpenDART 원자료(list of dict)이며, 계정과목(account_nm)·금액(thstrm_amount)
    ·재무제표구분(sj_div) 등을 포함한다. ROE/부채비율/FCF 파생은 배선 단계에서
    `derive_metrics` 로 계산한다(계정과목 표준화가 관건 — TODO 참고).

    :param corp_code: OpenDART 고유번호(8자리). corp_code_map 으로 획득.
    :param bsns_year: 사업연도(예: 2024)
    :param reprt_code: 보고서 코드(REPORT_ANNUAL 등)
    :param fs_div: 연결(CFS)/개별(OFS)
    """
    data = _get(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        },
    )
    if data is None:
        return None
    return data.get("list", [])


# 파생 계산에 쓰는 표준 계정 식별자(IFRS 택소노미 account_id).
# 한글 계정명(account_nm)은 회사/업종/작성기준별로 변형이 많지만(예: "당기순이익
# (손실)" vs "당기순이익"), account_id 는 IFRS 표준이라 훨씬 안정적이다. 따라서
# account_id 를 1순위로 매칭하고, id 가 비어 있는 구형·비표준 공시만 이름으로 폴백한다.
# 각 계정은 재무제표 구분(sj_div)으로도 제한한다 — 같은 id(예: ifrs-full_Equity,
# ifrs-full_ProfitLoss)가 BS/SCE, IS/CIS/CF/SCE 등 여러 표에 중복 등장하기 때문.
_EQUITY_ID = "ifrs-full_Equity"            # 자본총계 (BS)
_LIABILITIES_ID = "ifrs-full_Liabilities"  # 부채총계 (BS)
_ASSETS_ID = "ifrs-full_Assets"            # 자산총계 (BS)
_OP_INCOME_ID = "dart_OperatingIncomeLoss"  # 영업이익 (IS) — DART 확장 태그
_NET_INCOME_ID = "ifrs-full_ProfitLoss"    # 당기순이익 (IS/CIS). 지배/비지배 귀속분은
#   ...AttributableToOwners/Noncontrolling 로 id 가 달라 자동 배제된다.
_OCF_ID = "ifrs-full_CashFlowsFromUsedInOperatingActivities"  # 영업활동현금흐름 (CF)
# CAPEX(유·무형자산 취득)는 회사별로 account_id 접미사가 붙어(…ClassifiedAsInvesting
# Activities) 정확 일치가 어렵다 → prefix/이름 병행 매칭.
_CAPEX_PP_E_PREFIX = "ifrs-full_PurchaseOfPropertyPlantAndEquipment"
_CAPEX_INTANGIBLE_PREFIX = "ifrs-full_PurchaseOfIntangibleAssets"
# F-Score 확장용 계정.
_CURRENT_ASSETS_ID = "ifrs-full_CurrentAssets"          # 유동자산 (BS)
_CURRENT_LIABILITIES_ID = "ifrs-full_CurrentLiabilities"  # 유동부채 (BS)
_REVENUE_ID = "ifrs-full_Revenue"                        # 매출액/영업수익 (IS/CIS)
_GROSS_PROFIT_ID = "ifrs-full_GrossProfit"               # 매출총이익 (IS/CIS)


def _to_number(raw: object) -> float | None:
    """OpenDART 금액 문자열("455,905,980,000,000", "-", "")을 float 로 변환.

    콤마 제거, 빈 값/하이픈/변환 불가 시 None.
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick(
    accounts: list[dict],
    sj_div: str,
    *,
    account_id: str | None = None,
    id_prefix: str | None = None,
    name_contains: str | None = None,
    name_exclude: tuple[str, ...] = (),
) -> float | None:
    """지정한 재무제표 구분(sj_div)에서 계정 금액(thstrm_amount)을 뽑는다.

    매칭 우선순위: (1) account_id 정확 일치 → (2) id_prefix 접두 일치 →
    (3) name_contains 부분 일치. account_id 가 비어 있는 구형 공시를 위해 이름
    폴백을 둔다. name_exclude 에 걸리는 계정명은 폴백 대상에서 제외한다(예: 순이익
    이름 폴백 시 "지배기업의 소유주에게 귀속되는 당기순이익" 오매칭 방지). 첫
    매칭의 당기 금액을 반환.
    """
    fallback: float | None = None
    for row in accounts:
        if row.get("sj_div") != sj_div:
            continue
        rid = (row.get("account_id") or "").strip()
        nm = row.get("account_nm") or ""
        if account_id and rid == account_id:
            return _to_number(row.get("thstrm_amount"))
        if id_prefix and rid.startswith(id_prefix):
            return _to_number(row.get("thstrm_amount"))
        if (
            name_contains
            and name_contains in nm
            and not any(x in nm for x in name_exclude)
            and fallback is None
        ):
            fallback = _to_number(row.get("thstrm_amount"))
    return fallback


def _sum_pick(
    accounts: list[dict],
    sj_div: str,
    specs: list[dict],
) -> float | None:
    """여러 계정(예: 유형+무형자산 취득)을 각각 뽑아 합산한다.

    하나도 못 찾으면 None, 일부라도 찾으면 찾은 것들의 합.
    """
    total: float | None = None
    for spec in specs:
        v = _pick(accounts, sj_div, **spec)
        if v is not None:
            total = v if total is None else total + v
    return total


def derive_metrics(accounts: list[dict]) -> dict[str, float | None]:
    """전체 재무제표 원계정에서 핵심 재무지표를 파생한다.

    account_id(IFRS 표준 태그) 우선 매칭으로 계정과목명 변형 문제를 우회한다.
    계산 불가(계정 부재·자본총계 0/음수 등)한 항목은 None(호출부가 중립 처리).

    파생 공식:
      - roe         = 당기순이익 / 자본총계        (자본총계>0 일 때만)
      - debt_ratio  = 부채총계 / 자본총계 (배수, ×1) (자본총계>0 일 때만)
      - op_income   = 영업이익 (원화 그대로)
      - net_income  = 당기순이익 (원화 그대로)
      - fcf         = 영업활동현금흐름 − (유형자산 취득 + 무형자산 취득)
      - roa         = 당기순이익 / 자산총계        (F-Score 등 확장용, 자산>0)

    또한 F-Score(Piotroski) 계산을 위한 원자료도 함께 반환한다: cfo(영업활동현금흐름),
    assets·liabilities·current_assets·current_liabilities(재무상태표), revenue·gross_profit
    (손익). 이들은 다년치 비교(f_score)에서 사용된다.

    :param accounts: single_company_accounts 결과(list of dict). None/빈 리스트면 전부 None.
    :return: 파생지표 + F-Score 원자료 dict(float | None)
    """
    empty = {
        "roe": None, "debt_ratio": None, "op_income": None,
        "net_income": None, "fcf": None, "roa": None,
        "cfo": None, "assets": None, "liabilities": None,
        "current_assets": None, "current_liabilities": None,
        "revenue": None, "gross_profit": None,
    }
    if not accounts:
        return dict(empty)

    equity = _pick(accounts, "BS", account_id=_EQUITY_ID, name_contains="자본총계")
    liabilities = _pick(accounts, "BS", account_id=_LIABILITIES_ID, name_contains="부채총계")
    assets = _pick(accounts, "BS", account_id=_ASSETS_ID, name_contains="자산총계")
    # 손익 항목: 손익계산서(IS) 우선, 없으면 포괄손익계산서(CIS) 폴백. NAVER 등은
    # 별도 IS 없이 단일 CIS 로만 작성해 영업이익·순이익이 CIS 에 담긴다.
    op_income = _pick(accounts, "IS", account_id=_OP_INCOME_ID, name_contains="영업이익")
    if op_income is None:
        op_income = _pick(accounts, "CIS", account_id=_OP_INCOME_ID, name_contains="영업이익")
    # 순이익 이름 폴백은 귀속분(지배/비지배)·차감전 순이익을 배제해 본계정만 잡는다.
    _NI_EXCLUDE = ("귀속", "지배기업", "차감전", "비지배")
    net_income = _pick(accounts, "IS", account_id=_NET_INCOME_ID,
                       name_contains="당기순이익", name_exclude=_NI_EXCLUDE)
    if net_income is None:
        net_income = _pick(accounts, "CIS", account_id=_NET_INCOME_ID,
                           name_contains="당기순이익", name_exclude=_NI_EXCLUDE)

    ocf = _pick(accounts, "CF", account_id=_OCF_ID, name_contains="영업활동현금흐름")
    capex = _sum_pick(accounts, "CF", [
        {"id_prefix": _CAPEX_PP_E_PREFIX, "name_contains": "유형자산의 취득"},
        {"id_prefix": _CAPEX_INTANGIBLE_PREFIX, "name_contains": "무형자산의 취득"},
    ])

    # F-Score 원자료(재무상태표 유동항목·손익 매출/매출총이익).
    current_assets = _pick(accounts, "BS", account_id=_CURRENT_ASSETS_ID,
                           name_contains="유동자산", name_exclude=("비유동",))
    current_liabilities = _pick(accounts, "BS", account_id=_CURRENT_LIABILITIES_ID,
                                name_contains="유동부채", name_exclude=("비유동",))
    revenue = _pick(accounts, "IS", account_id=_REVENUE_ID, name_contains="매출액")
    if revenue is None:
        revenue = _pick(accounts, "CIS", account_id=_REVENUE_ID, name_contains="매출액")
    gross_profit = _pick(accounts, "IS", account_id=_GROSS_PROFIT_ID, name_contains="매출총이익")
    if gross_profit is None:
        gross_profit = _pick(accounts, "CIS", account_id=_GROSS_PROFIT_ID, name_contains="매출총이익")

    roe = net_income / equity if (net_income is not None and equity and equity > 0) else None
    debt_ratio = liabilities / equity if (liabilities is not None and equity and equity > 0) else None
    roa = net_income / assets if (net_income is not None and assets and assets > 0) else None
    fcf = ocf - capex if (ocf is not None and capex is not None) else None

    return {
        "roe": roe,
        "debt_ratio": debt_ratio,
        "op_income": op_income,
        "net_income": net_income,
        "fcf": fcf,
        "roa": roa,
        "cfo": ocf,
        "assets": assets,
        "liabilities": liabilities,
        "current_assets": current_assets,
        "current_liabilities": current_liabilities,
        "revenue": revenue,
        "gross_profit": gross_profit,
    }


def piotroski_f_score(cur: dict, prev: dict) -> int | None:
    """수정 Piotroski F-Score(8점)를 계산한다(당해 cur, 전년 prev 파생지표 dict).

    표준 9점 중 '신주 미발행'은 재무제표만으론 신뢰도 있게 판정하기 어려워 제외한
    8점 버전이다(발행주식수는 외부 소스 필요). 각 항목은 필요한 원자료가 있을 때만
    가점하며, 계산 가능한 항목이 5개 미만이면 신뢰도 부족으로 None 을 반환한다.

    항목(각 +1):
      수익성 — ROA>0, CFO>0, ΔROA>0, 발생액(CFO/자산 > ROA)
      레버리지/유동성 — Δ레버리지<0(부채/자산 감소), Δ유동비율>0
      운영효율 — Δ매출총이익률>0, Δ자산회전율>0
    """
    score = 0
    computable = 0

    def ratio(a, b):
        if a is None or b is None or b == 0:
            return None
        return a / b

    roa_t, roa_p = cur.get("roa"), prev.get("roa")
    cfo_t, assets_t = cur.get("cfo"), cur.get("assets")

    # 1) ROA_t > 0
    if roa_t is not None:
        computable += 1
        if roa_t > 0:
            score += 1
    # 2) CFO_t > 0
    if cfo_t is not None:
        computable += 1
        if cfo_t > 0:
            score += 1
    # 3) ΔROA > 0
    if roa_t is not None and roa_p is not None:
        computable += 1
        if roa_t > roa_p:
            score += 1
    # 4) 발생액: CFO/자산 > ROA (현금이익질)
    cfo_ta = ratio(cfo_t, assets_t)
    if cfo_ta is not None and roa_t is not None:
        computable += 1
        if cfo_ta > roa_t:
            score += 1
    # 5) Δ레버리지 < 0 (총부채/자산 감소)
    lev_t = ratio(cur.get("liabilities"), cur.get("assets"))
    lev_p = ratio(prev.get("liabilities"), prev.get("assets"))
    if lev_t is not None and lev_p is not None:
        computable += 1
        if lev_t < lev_p:
            score += 1
    # 6) Δ유동비율 > 0
    cr_t = ratio(cur.get("current_assets"), cur.get("current_liabilities"))
    cr_p = ratio(prev.get("current_assets"), prev.get("current_liabilities"))
    if cr_t is not None and cr_p is not None:
        computable += 1
        if cr_t > cr_p:
            score += 1
    # 7) Δ매출총이익률 > 0
    gm_t = ratio(cur.get("gross_profit"), cur.get("revenue"))
    gm_p = ratio(prev.get("gross_profit"), prev.get("revenue"))
    if gm_t is not None and gm_p is not None:
        computable += 1
        if gm_t > gm_p:
            score += 1
    # 8) Δ자산회전율 > 0
    at_t = ratio(cur.get("revenue"), cur.get("assets"))
    at_p = ratio(prev.get("revenue"), prev.get("assets"))
    if at_t is not None and at_p is not None:
        computable += 1
        if at_t > at_p:
            score += 1

    if computable < 5:
        return None
    return score


def latest_report_period(as_of: date) -> tuple[int, str]:
    """as_of 시점에 '이미 공시되어 사용 가능한' 최신 (사업연도, 보고서코드).

    분기 PIT 세분 — 각 보고서의 보수적 공시 마감(제출기한)을 기준으로 판정한다:
      - 1분기(11013): 5월 중순  → 5/16 이후 당해 1Q 사용
      - 반기(11012):  8월 중순  → 8/16 이후 당해 반기 사용
      - 3분기(11014): 11월 중순 → 11/16 이후 당해 3Q 사용
      - 사업보고서(11011): 이듬해 3월 말 → 4/1 이후 직전연도 연간 사용
    그 이전(연초~3월)은 재작년 연간이 최신 확정 공시다.

    예) 2025-05-20 → (2025, 1Q), 2025-09-01 → (2025, 반기),
        2025-03-10 → (2023, 연간), 2025-04-10 → (2024, 연간).
    """
    y, m, d = as_of.year, as_of.month, as_of.day
    after = lambda mm, dd: (m, d) >= (mm, dd)  # noqa: E731
    if after(11, 16):
        return (y, REPORT_Q3)
    if after(8, 16):
        return (y, REPORT_HALF)
    if after(5, 16):
        return (y, REPORT_Q1)
    if after(4, 1):
        return (y - 1, REPORT_ANNUAL)
    return (y - 2, REPORT_ANNUAL)


def announcement_lagged_year(as_of: date) -> int:
    """as_of 시점에 '이미 공시되어 사용 가능한' 최신 연간 사업보고서의 사업연도.

    룩어헤드 방지용 보수적 규칙: 사업보고서(연간)는 이듬해 3월 말 공시되므로,
    4월 이후에야 직전 연도 실적을 사용 가능하다고 본다. (분기 지연은 배선 단계에서
    reprt_code 별로 세분화: 1Q 5월중순 / 2Q 8월중순 / 3Q 11월중순.)

    예) as_of=2025-02-10 → 2023, as_of=2025-05-01 → 2024.
    """
    return as_of.year - 1 if as_of.month >= 4 else as_of.year - 2


# ─────────────────────── 캐시된 PIT 지표 provider ───────────────────────
# OpenDART 는 일 20,000건 요율에 종목당 개별 호출이라, 백테스트(리밸런싱일마다
# 유니버스 전 종목 조회)에서 그대로 호출하면 금방 한도를 넘고 느리다. 그래서
# corp_code 매핑(하루 1회 수준)과 연간 재무제표(사업연도 단위 불변)를 프로세스 내
# 캐시한다. 워커 수명 동안 유지되며, 실패(None)는 캐시하지 않아 다음에 재시도된다.
_CORP_MAP_CACHE: dict[str, str] | None = None
_ACCOUNTS_CACHE: dict[tuple, list[dict]] = {}
_METRICS_CACHE: dict[tuple, dict[str, float | None]] = {}


def cached_corp_code_map() -> dict[str, str] | None:
    """corp_code_map 을 프로세스 내 1회만 로드해 재사용한다(대용량 zip 반복 다운로드 방지)."""
    global _CORP_MAP_CACHE
    if _CORP_MAP_CACHE is None:
        _CORP_MAP_CACHE = corp_code_map()
    return _CORP_MAP_CACHE


def annual_metrics(corp_code: str, bsns_year: int) -> dict[str, float | None]:
    """단일 종목·사업연도의 파생 재무지표(연결 우선·개별 폴백)를 캐시와 함께 반환한다.

    연결(CFS)로 먼저 조회하고, 무자료면 개별(OFS)로 폴백한다(지주사·비연결 회사 대비).
    (corp_code, year) 단위로 캐시한다. 조회 실패/무자료면 전 항목 None.
    """
    key = (corp_code, bsns_year)
    cached = _METRICS_CACHE.get(key)
    if cached is not None:
        return cached

    accounts: list[dict] | None = None
    for fs_div in (FS_CONSOLIDATED, FS_SEPARATE):
        acc_key = (corp_code, bsns_year, REPORT_ANNUAL, fs_div)
        acc = _ACCOUNTS_CACHE.get(acc_key)
        if acc is None:
            acc = single_company_accounts(corp_code, bsns_year, REPORT_ANNUAL, fs_div)
            if acc:
                _ACCOUNTS_CACHE[acc_key] = acc
        if acc:
            accounts = acc
            break

    metrics = derive_metrics(accounts or [])
    _METRICS_CACHE[key] = metrics
    return metrics


def metrics_by_symbol(codes: list[str], as_of: date) -> dict[str, dict[str, float | None]]:
    """유니버스 종목들의 PIT 재무지표를 {종목코드: {roe,debt_ratio,fcf,...}} 로 반환한다.

    미래참조 방지: as_of 기준으로 announcement_lagged_year 가 산정한 '이미 공시된'
    사업연도의 연간 재무제표만 사용한다. 키 미승인/조회 실패 종목은 결과에서 제외된다
    (호출부가 중립 처리). 블로킹 함수이므로 호출자는 스레드풀에서 실행한다.
    """
    if not is_enabled() or not codes:
        return {}
    corp_map = cached_corp_code_map()
    if not corp_map:
        return {}

    year = announcement_lagged_year(as_of)
    out: dict[str, dict[str, float | None]] = {}
    for raw in codes:
        code = str(raw).strip().zfill(6)
        corp = corp_map.get(code)
        if not corp:
            continue
        m = dict(annual_metrics(corp, year))
        # 전년 실적으로 YoY 성장·흑자전환(실적상향/턴어라운드 팩터)·F-Score를 파생한다.
        prev = annual_metrics(corp, year - 1)
        m["op_growth"] = _yoy_growth(m.get("op_income"), prev.get("op_income"))
        m["net_growth"] = _yoy_growth(m.get("net_income"), prev.get("net_income"))
        m["turnaround"] = _turnaround_flag(m.get("net_income"), prev.get("net_income"))
        m["f_score"] = piotroski_f_score(m, prev)
        # 최근 3년 순이익(만성 적자 판정용). 전 항목 None(조회 실패)이면 미수록.
        prev2 = annual_metrics(corp, year - 2)
        m["loss_years_3"] = _count_losses(
            [m.get("net_income"), prev.get("net_income"), prev2.get("net_income")]
        )
        if any(v is not None for v in m.values()):
            out[code] = m
    return out


def _count_losses(net_incomes: list[float | None]) -> int | None:
    """순이익 리스트에서 적자(≤0)인 해의 수를 센다. 값이 하나도 없으면 None."""
    vals = [v for v in net_incomes if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v <= 0)


def _yoy_growth(cur: float | None, prev: float | None) -> float | None:
    """전년 대비 성장률. 전년이 0 또는 적자(≤0)면 비율이 왜곡되므로 None(중립).

    (예: 흑자전환은 성장률로 표현 불가 → 별도 turnaround 플래그로 다룬다.)
    """
    if cur is None or prev is None or prev <= 0:
        return None
    return cur / prev - 1.0


def _turnaround_flag(net_cur: float | None, net_prev: float | None) -> float | None:
    """흑자전환 여부(전년 적자 → 당해 흑자)를 1.0/0.0 으로. 데이터 부족 시 None."""
    if net_cur is None or net_prev is None:
        return None
    return 1.0 if (net_prev <= 0 and net_cur > 0) else 0.0
