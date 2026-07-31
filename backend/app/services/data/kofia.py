"""금융투자협회 FreeSIS 증시자금 클라이언트 — 미수금·반대매매 일별 시계열.

목적: 시장 **취약성**(레버리지 축적·강제청산 압력)을 관측하기 위한 소스. 기존
패닉 지표(`app/services/metrics/panic.py`)는 가격·거래대금·브레드스 기반이라
설계상 **동시지표**이며, 그 docstring 이 밝히듯 "자본항복은 바닥 근처에 발생"해
사전 경보로 쓸 수 없다. 이 모듈은 그 앞단을 채우기 위한 데이터 계층이다.

## 왜 FreeSIS 인가
KRX MDC STAT 계열(`data.krx.co.kr`)은 KRX 로그인 세션을 요구하지만, FreeSIS 는
**인증 없이** 열리고 2008년까지 소급된다(2008 금융위기·2011·2020·2022 구간 확인).
과거 전 국면 오탐률 측정에 필요한 이력이 확보된다는 뜻이다.

## 컬럼 식별 (중요)
FreeSIS 응답에는 **컬럼 라벨이 없다**(`dsmHeader`·`unit` 모두 빈 문자열). 아래 셋은
2026-06~07 42거래일 궤적의 산술관계로 확정했다 — 반대매매 비중의 정의가
`반대매매금액[t] / 미수금[t-1] × 100` 이라는 관계가 41/41 성립(최대 오차 0.05%p)했고,
당일 미수금 기준 가설은 17/42 로 기각됐다.

- `TMPV5` 위탁매매 미수금(원)
- `TMPV6` 반대매매 금액(원)
- `TMPV7` 미수금 대비 반대매매 비중(%) — **전일** 미수금이 분모

`TMPV2`(투자자예탁금 추정)·`TMPV3`·`TMPV4` 는 **미확정이라 이름을 붙이지 않고**
`raw` 로만 통과시킨다. 확인되지 않은 의미를 필드명으로 박으면 그 자체가 오류의
출처가 된다. 신용융자 잔고는 이 통계표에 없다(별도 OBJ_NM 미발견).

## 해석 주의 — 단독 사용 금지
반대매매 비중은 스트레스는 잡지만 **낙폭 규모는 구분하지 못한다.** 실측:
2022-06 긴축장 평균 8.7·최대 13.1 이 2026-07 폭락(최대 10.5)보다 높았고,
2026-06-09 에 10.5 를 찍은 뒤에도 지수는 6-22 사상 최고까지 올랐다.
단독 게이트로 쓰면 오탐이 확실하다 — 반드시 다른 취약성 지표와 결합하고
과거 전 국면 오탐률을 먼저 측정할 것.

## 규약
- 새 외부 의존성 없음(httpx 는 기존 의존).
- 데이터 계층 공통 규약대로 **블로킹(sync)** 함수다. 호출자가 스레드풀에서 실행한다.
- 실패·무자료 시 예외 대신 빈 리스트를 반환한다(호출부가 중립 처리).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import httpx

logger = logging.getLogger(__name__)

_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
# 증시자금 추이(일별) 통계표.
_OBJ_MARKET_FUNDS = "STATSCU0100000060BO"
_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://freesis.kofia.or.kr/",
}
_TIMEOUT = 20.0


@dataclass
class MarketFunds:
    """증시자금 일별 관측 1건.

    :param as_of: 관측일
    :param receivable: 위탁매매 미수금(원)
    :param forced_sale_amount: 반대매매 금액(원)
    :param forced_sale_ratio: 미수금 대비 반대매매 비중(%). 분모는 **전일** 미수금
    :param raw: 원본 행. 의미가 확정되지 않은 컬럼(TMPV2~TMPV4)을 잃지 않기 위해 보존
    """

    as_of: date
    receivable: float | None
    forced_sale_amount: float | None
    forced_sale_ratio: float | None
    raw: dict = field(default_factory=dict)


def _f(row: dict, key: str) -> float | None:
    """숫자 필드를 float 로. 값이 없거나 숫자가 아니면 None(0 으로 채우지 않는다)."""
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ymd(v: object) -> date | None:
    s = str(v or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


def fetch_market_funds(start: date, end: date) -> list[MarketFunds]:
    """[start, end] 구간의 증시자금 일별 시계열을 날짜 오름차순으로 반환한다.

    실패하면 경고만 남기고 빈 리스트를 반환한다 — 취약성 관측이 없다고 해서
    매매나 백테스트가 멈추면 안 된다.
    """
    if start > end:
        raise ValueError(f"start 가 end 보다 늦다: {start} > {end}")

    payload = {
        "dmSearch": {
            "tmpV40": "1",
            "tmpV41": "1",
            "tmpV45": start.strftime("%Y%m%d"),
            "tmpV46": end.strftime("%Y%m%d"),
            "OBJ_NM": _OBJ_MARKET_FUNDS,
        }
    }
    try:
        resp = httpx.post(_URL, json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("ds1") or []
    except Exception as e:  # noqa: BLE001 - 외부 소스 장애를 상위로 전파하지 않는다
        logger.warning("FreeSIS 증시자금 조회 실패(%s~%s): %s", start, end, e)
        return []

    out: list[MarketFunds] = []
    for r in rows:
        as_of = _parse_ymd(r.get("TMPV1"))
        if as_of is None:
            continue
        out.append(
            MarketFunds(
                as_of=as_of,
                receivable=_f(r, "TMPV5"),
                forced_sale_amount=_f(r, "TMPV6"),
                forced_sale_ratio=_f(r, "TMPV7"),
                raw=dict(r),
            )
        )
    out.sort(key=lambda m: m.as_of)
    return out
