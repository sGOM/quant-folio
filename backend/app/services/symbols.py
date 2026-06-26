"""KRX 종목 검색 — 코드/한글명/영문명으로 종목을 찾는다.

내장 주요 종목 목록(영문명 포함)을 기본으로 하고, 가능하면 FinanceDataReader
의 KRX 전체 상장 목록(코드·한글명)을 병합해 검색 범위를 넓힌다. 네트워크가
없거나 조회에 실패해도 내장 목록만으로 동작한다(graceful degradation).

검색은 코드/한글명/영문명 부분일치(대소문자 무시)이며, 일치 품질로 정렬한다.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# 내장 주요 종목(코드, 한글명, 영문명, 시장). 영문 검색은 이 목록에서 매칭된다.
# FDR 전체 목록을 병합해도 영문명은 대개 비어 있어, 자주 검색하는 대형주는 여기에 둔다.
_SEED: list[tuple[str, str, str, str]] = [
    ("005930", "삼성전자", "Samsung Electronics", "KOSPI"),
    ("000660", "SK하이닉스", "SK Hynix", "KOSPI"),
    ("373220", "LG에너지솔루션", "LG Energy Solution", "KOSPI"),
    ("207940", "삼성바이오로직스", "Samsung Biologics", "KOSPI"),
    ("005380", "현대차", "Hyundai Motor", "KOSPI"),
    ("000270", "기아", "Kia", "KOSPI"),
    ("005935", "삼성전자우", "Samsung Electronics Pref", "KOSPI"),
    ("068270", "셀트리온", "Celltrion", "KOSPI"),
    ("105560", "KB금융", "KB Financial Group", "KOSPI"),
    ("055550", "신한지주", "Shinhan Financial Group", "KOSPI"),
    ("035420", "NAVER", "Naver", "KOSPI"),
    ("035720", "카카오", "Kakao", "KOSPI"),
    ("012330", "현대모비스", "Hyundai Mobis", "KOSPI"),
    ("028260", "삼성물산", "Samsung C&T", "KOSPI"),
    ("006400", "삼성SDI", "Samsung SDI", "KOSPI"),
    ("051910", "LG화학", "LG Chem", "KOSPI"),
    ("003670", "포스코퓨처엠", "POSCO Future M", "KOSPI"),
    ("005490", "POSCO홀딩스", "POSCO Holdings", "KOSPI"),
    ("015760", "한국전력", "KEPCO", "KOSPI"),
    ("017670", "SK텔레콤", "SK Telecom", "KOSPI"),
    ("030200", "KT", "KT", "KOSPI"),
    ("033780", "KT&G", "KT&G", "KOSPI"),
    ("066570", "LG전자", "LG Electronics", "KOSPI"),
    ("003550", "LG", "LG Corp", "KOSPI"),
    ("034730", "SK", "SK Inc", "KOSPI"),
    ("096770", "SK이노베이션", "SK Innovation", "KOSPI"),
    ("009150", "삼성전기", "Samsung Electro-Mechanics", "KOSPI"),
    ("032830", "삼성생명", "Samsung Life Insurance", "KOSPI"),
    ("086790", "하나금융지주", "Hana Financial Group", "KOSPI"),
    ("316140", "우리금융지주", "Woori Financial Group", "KOSPI"),
    ("000810", "삼성화재", "Samsung Fire & Marine", "KOSPI"),
    ("011200", "HMM", "HMM", "KOSPI"),
    ("010130", "고려아연", "Korea Zinc", "KOSPI"),
    ("009830", "한화솔루션", "Hanwha Solutions", "KOSPI"),
    ("011170", "롯데케미칼", "Lotte Chemical", "KOSPI"),
    ("259960", "크래프톤", "Krafton", "KOSPI"),
    ("036570", "엔씨소프트", "NCSoft", "KOSPI"),
    ("251270", "넷마블", "Netmarble", "KOSPI"),
    ("018260", "삼성에스디에스", "Samsung SDS", "KOSPI"),
    ("010950", "S-Oil", "S-Oil", "KOSPI"),
    ("090430", "아모레퍼시픽", "Amorepacific", "KOSPI"),
    ("051900", "LG생활건강", "LG H&H", "KOSPI"),
    ("024110", "기업은행", "Industrial Bank of Korea", "KOSPI"),
    ("267260", "HD현대일렉트릭", "HD Hyundai Electric", "KOSPI"),
    ("329180", "HD현대중공업", "HD Hyundai Heavy Industries", "KOSPI"),
    ("042660", "한화오션", "Hanwha Ocean", "KOSPI"),
    ("064350", "현대로템", "Hyundai Rotem", "KOSPI"),
    ("012450", "한화에어로스페이스", "Hanwha Aerospace", "KOSPI"),
    # KOSDAQ 대형주
    ("247540", "에코프로비엠", "Ecopro BM", "KOSDAQ"),
    ("086520", "에코프로", "Ecopro", "KOSDAQ"),
    ("091990", "셀트리온헬스케어", "Celltrion Healthcare", "KOSDAQ"),
    ("196170", "알테오젠", "Alteogen", "KOSDAQ"),
    ("028300", "HLB", "HLB", "KOSDAQ"),
    ("066970", "엘앤에프", "L&F", "KOSDAQ"),
    ("058470", "리노공업", "Leeno Industrial", "KOSDAQ"),
    ("357780", "솔브레인", "Soulbrain", "KOSDAQ"),
    ("293490", "카카오게임즈", "Kakao Games", "KOSDAQ"),
    ("263750", "펄어비스", "Pearl Abyss", "KOSDAQ"),
    ("041510", "에스엠", "SM Entertainment", "KOSDAQ"),
    ("035900", "JYP Ent.", "JYP Entertainment", "KOSDAQ"),
    ("253450", "스튜디오드래곤", "Studio Dragon", "KOSDAQ"),
]


# 병합된 카탈로그 캐시(프로세스 1회 빌드). dict[code] -> {code, name, name_en, market}
_cache: list[dict[str, str]] | None = None


def _build_catalog() -> list[dict[str, str]]:
    """내장 목록 + (가능하면) FDR KRX 전체 목록을 병합해 카탈로그를 만든다."""
    by_code: dict[str, dict[str, str]] = {
        code: {"code": code, "name": name, "name_en": en, "market": market}
        for code, name, en, market in _SEED
    }

    try:
        import FinanceDataReader as fdr

        df = fdr.StockListing("KRX")
        # 컬럼명은 버전에 따라 다를 수 있어 방어적으로 접근한다.
        cols = {c.lower(): c for c in df.columns}
        code_col = cols.get("code") or cols.get("symbol")
        name_col = cols.get("name")
        market_col = cols.get("market")
        if code_col and name_col:
            for _, row in df.iterrows():
                code = str(row[code_col]).strip()
                # 보통주(숫자 6자리)만; 우선주/스팩 등도 코드가 6자리라 그대로 둔다.
                if not code or len(code) != 6 or not code.isdigit():
                    continue
                name = str(row[name_col]).strip()
                market = str(row[market_col]).strip() if market_col else ""
                if code in by_code:
                    # 내장 항목은 영문명을 보존하고 시장만 보강.
                    if market and not by_code[code]["market"]:
                        by_code[code]["market"] = market
                else:
                    by_code[code] = {
                        "code": code,
                        "name": name,
                        "name_en": "",
                        "market": market,
                    }
            logger.info("종목 카탈로그 로드: %d개 (FDR 병합)", len(by_code))
    except Exception as e:  # noqa: BLE001
        logger.warning("FDR 상장목록 조회 실패, 내장 목록만 사용: %s", e)

    return list(by_code.values())


def get_catalog() -> list[dict[str, str]]:
    """종목 카탈로그를 반환한다(최초 호출 시 빌드 후 프로세스 캐시).

    동기(blocking·네트워크 포함) 함수이므로 호출자는 스레드풀에서 실행할 것.
    """
    global _cache
    if _cache is None:
        _cache = _build_catalog()
    return _cache


def search_symbols(query: str, limit: int = 20) -> list[dict[str, str]]:
    """코드/한글명/영문명으로 종목을 검색해 일치 품질 순으로 반환한다.

    :param query: 검색어(종목코드 일부, 한글명, 영문명)
    :param limit: 최대 반환 개수
    :return: [{code, name, name_en, market}] 정렬된 목록
    """
    q = query.strip().lower()
    if not q:
        return []

    catalog = get_catalog()
    scored: list[tuple[int, dict[str, str]]] = []
    for item in catalog:
        code = item["code"].lower()
        name = item["name"].lower()
        en = item["name_en"].lower()

        # 점수: 낮을수록 우선. 코드 정확일치 > 코드 접두 > 이름 접두 > 부분일치.
        if code == q:
            score = 0
        elif code.startswith(q):
            score = 1
        elif name.startswith(q) or (en and en.startswith(q)):
            score = 2
        elif q in name or (en and q in en):
            score = 3
        elif q in code:
            score = 4
        else:
            continue
        scored.append((score, item))

    scored.sort(key=lambda t: (t[0], t[1]["code"]))
    return [item for _, item in scored[:limit]]
