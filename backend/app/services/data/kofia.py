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
출처가 된다.

신용융자 잔고는 이 통계표가 아니라 별도 통계표(`_OBJ_CREDIT`)에 있다 —
`fetch_credit_balance` 참고. 이쪽이 취약성 관측의 1순위 지표다.

## 해석 주의 — 단독 사용 금지
반대매매 비중은 스트레스는 잡지만 **낙폭 규모는 구분하지 못한다.** 실측:
2022-06 긴축장 평균 8.7·최대 13.1 이 2026-07 폭락(최대 10.5)보다 높았고,
2026-06-09 에 10.5 를 찍은 뒤에도 지수는 6-22 사상 최고까지 올랐다.
단독 게이트로 쓰면 오탐이 확실하다 — 반드시 다른 취약성 지표와 결합하고
과거 전 국면 오탐률을 먼저 측정할 것.

## 규약
- 새 외부 의존성 없음(httpx 는 기존 의존).
- 데이터 계층 공통 규약대로 **블로킹(sync)** 함수다. 호출자가 스레드풀에서 실행한다.
- **실패와 무자료를 가른다**: 전송·스키마 실패는 `app.services.data.errors` 의 원인별
  `DataSourceError` 로 전파하고, 정상 응답이면서 행이 없는 것(조회 구간에 자료 없음)만
  빈 리스트다. 둘을 모두 빈 리스트로 뭉개면 호출부가 '취약성 없음'과 '관측 실패'를
  구분할 수 없어, 레버리지가 쌓이는 국면을 조용히 놓친다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import httpx

from app.services.data.errors import (
    SourceQuotaError,
    SourceSchemaError,
    classify_httpx,
    cooldown_remaining,
    note_failure,
)

logger = logging.getLogger(__name__)

_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
# 증시자금 추이(일별) 통계표.
_OBJ_MARKET_FUNDS = "STATSCU0100000060BO"
# 신용공여(신용융자) 잔고 추이(일별) 통계표.
_OBJ_CREDIT = "STATSCU0100000070BO"
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


@dataclass
class CreditBalance:
    """신용융자 잔고 일별 관측 1건.

    :param as_of: 관측일
    :param total: 신용융자 잔고 합계(원). **취약성 관측의 1순위 지표** — 이 잔고가
        쌓이는 과정이 곧 강제청산 압력의 축적이며, 가격이 오르는 동안 관측된다
    :param part_a: 합계의 구성요소 1(원). 개별 의미 미확정 — 아래 주석 참고
    :param part_b: 합계의 구성요소 2(원). 개별 의미 미확정
    :param raw: 원본 행(신용대주·담보융자로 보이는 잔여 컬럼 보존)
    """

    as_of: date
    total: float | None
    part_a: float | None
    part_b: float | None
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


def _rows(obj_nm: str, start: date, end: date, label: str) -> list[dict]:
    """FreeSIS 통계표 1건을 조회해 원본 행 리스트를 반환한다.

    실패는 원인별 DataSourceError 로 raise 한다. 정상 응답이면서 행이 비어 있는 것은
    실패가 아니다(조회 구간에 자료가 없는 정상 상황).

    :raises DataSourceError: 쿨다운 중, 전송 실패(원인별), 응답에 'ds1' 키 부재(스키마)
    """
    if start > end:
        raise ValueError(f"start 가 end 보다 늦다: {start} > {end}")

    # 쿨다운 검사는 호출 직전 한 곳(여기)에서만 한다 — 걸어두기만 하고 아무도 읽지
    # 않으면 죽은 코드이고, FreeSIS 장애 시 매 호출이 타임아웃(20초)을 새로 소진한다.
    # 이미 걸린 쿨다운을 검사만 하고 갱신하지는 않는다(여기서 note_failure 하면
    # 호출이 들어오는 한 쿨다운이 끝없이 연장된다). 선례: opendart._get
    remaining = cooldown_remaining("kofia")
    if remaining > 0:
        raise SourceQuotaError("kofia", f"쿨다운 중 — 조회 생략({remaining:.0f}초 남음)")

    payload = {
        "dmSearch": {
            "tmpV40": "1",
            "tmpV41": "1",
            "tmpV45": start.strftime("%Y%m%d"),
            "tmpV46": end.strftime("%Y%m%d"),
            "OBJ_NM": obj_nm,
        }
    }
    try:
        resp = httpx.post(_URL, json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        exc = classify_httpx("kofia", e)
        note_failure(exc)
        logger.warning("FreeSIS %s 조회 실패(%s~%s): %s", label, start, end, exc)
        raise exc from e

    # 'ds1' 키 자체가 없으면 응답 구조가 우리 파서의 계약과 다르다 — 쿨다운으로
    # 은폐하지 않고 코드 수정을 요구한다(SourceSchemaError 는 쿨다운을 걸지 않는다).
    if not isinstance(data, dict) or "ds1" not in data:
        raise SourceSchemaError("kofia", f"{label} 응답에 'ds1' 키가 없다: {str(data)[:120]}")
    return data.get("ds1") or []


def fetch_credit_balance(start: date, end: date) -> list[CreditBalance]:
    """[start, end] 구간의 신용융자 잔고 일별 시계열을 날짜 오름차순으로 반환한다.

    ## 컬럼 식별
    - `TMPV2` **신용융자 잔고 합계**. 2026-06-01 37.68조 → 07-30 32.15조(−14.7%)로
      같은 구간 보도치(38조→32조, −15.4%)와 일치한다.
    - `TMPV3`·`TMPV4` 는 합계의 구성요소다 — `TMPV2 == TMPV3 + TMPV4` 가 42/42
      성립(상대오차 0.1% 미만). 다만 **각각이 무엇인지는 확정하지 못했다**(시장별
      분해인지, 신용융자/담보융자 분해인지). 2026-06~07 에 TMPV3 −8.4% 대 TMPV4
      −32.4% 로 감소폭이 크게 갈렸다는 사실만 관측됐다. 이름을 붙이지 않고
      `part_a`/`part_b` 로 둔다.
    - 나머지 컬럼(신용대주·예탁증권담보융자로 **추정**)은 `raw` 로만 보존한다.

    전송 실패는 DataSourceError 로 전파된다. 빈 리스트는 '조회 구간에 자료가 없음'
    뿐이다(정상 응답).

    :raises DataSourceError: 조회 실패(_rows 참고)
    """
    out: list[CreditBalance] = []
    for r in _rows(_OBJ_CREDIT, start, end, "신용융자 잔고"):
        as_of = _parse_ymd(r.get("TMPV1"))
        if as_of is None:
            continue
        out.append(
            CreditBalance(
                as_of=as_of,
                total=_f(r, "TMPV2"),
                part_a=_f(r, "TMPV3"),
                part_b=_f(r, "TMPV4"),
                raw=dict(r),
            )
        )
    out.sort(key=lambda c: c.as_of)
    return out


def fetch_market_funds(start: date, end: date) -> list[MarketFunds]:
    """[start, end] 구간의 증시자금 일별 시계열을 날짜 오름차순으로 반환한다.

    전송 실패는 DataSourceError 로 전파된다 — 관측이 '없는' 것과 관측에 '실패한'
    것은 다르다. 후자를 빈 리스트로 뭉개면 호출부가 취약성 0 으로 오독한다.

    :raises DataSourceError: 조회 실패(_rows 참고)
    """
    out: list[MarketFunds] = []
    for r in _rows(_OBJ_MARKET_FUNDS, start, end, "증시자금"):
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
