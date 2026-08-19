"""KIS 종목마스터(거래정지·관리종목·액면가·업종분류 등) 클라이언트.

목적: KRX MDC/FDR/DART 어디에도 없는 매매 상태 플래그(거래정지·관리종목·
정리매매·시장경고·불성실공시·우회상장·단기과열·SPAC)와 액면가·업종 세분류를
로컬에 확보한다.

데이터 소스: KIS 가 공개 CDN 으로 배포하는 시장 전체 zip 파일
(`https://new.real.download.dws.co.kr/common/master/{kospi,kosdaq}_code.mst.zip`).
**인증·유량제한이 없다** — KIS 앱키/토큰과 무관한 별도 경로다. 필드 스펙은
KIS 공식 GitHub(`koreainvestment/open-trading-api`)의
`stocks_info/kis_kospi_code_mst.py`·`kis_kosdaq_code_mst.py`에서 이식했다.

설계 원칙(kofia.py 와 동일):
- 블로킹(sync) 함수 — 호출부가 스레드풀/asyncio.to_thread 로 실행.
- 전송·스키마 실패는 `app.services.data.errors` 의 원인별 `DataSourceError` 로
  raise 한다. 소스명은 `"kis_master"`.

상세: docs/superpowers/specs/2026-08-18-kis-stock-master-cache-design.md
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date

import httpx
import pandas as pd

from app.services.data.errors import (
    DataSourceError,
    SourceSchemaError,
    SourceUnavailableError,
    classify_httpx,
    cooldown_remaining,
    note_failure,
    representative,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://new.real.download.dws.co.kr/common/master/{name}.mst.zip"
_FILE_NAMES = {"KOSPI": "kospi_code", "KOSDAQ": "kosdaq_code"}
_TIMEOUT = 30.0

# ─────────────────────────── 코스피 필드 스펙 ───────────────────────────
# KIS 공식 레포 stocks_info/kis_kospi_code_mst.py 의 field_specs/part2_columns 그대로.
_KOSPI_FIELD_SPECS: list[int] = [
    2, 1, 4, 4, 4,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 9, 5, 5, 1,
    1, 1, 2, 1, 1,
    1, 2, 2, 2, 3,
    1, 3, 12, 12, 8,
    15, 21, 2, 7, 1,
    1, 1, 1, 1, 9,
    9, 9, 5, 9, 8,
    9, 3, 1, 1, 1,
]
_KOSPI_COLUMNS: list[str] = [
    "그룹코드", "시가총액규모", "지수업종대분류", "지수업종중분류", "지수업종소분류",
    "제조업", "저유동성", "지배구조지수종목", "KOSPI200섹터업종", "KOSPI100",
    "KOSPI50", "KRX", "ETP", "ELW발행", "KRX100",
    "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC",
    "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설",
    "Non1", "KRX증권", "KRX선박", "KRX섹터_보험", "KRX섹터_운송",
    "SRI", "기준가", "매매수량단위", "시간외수량단위", "거래정지",
    "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시",
    "우회상장", "락구분", "액면변경", "증자구분", "증거금비율",
    "신용가능", "신용기간", "전일거래량", "액면가", "상장일자",
    "상장주수", "자본금", "결산월", "공모가", "우선주",
    "공매도과열", "이상급등", "KRX300", "KOSPI", "매출액",
    "영업이익", "경상이익", "당기순이익", "ROE", "기준년월",
    "시가총액", "그룹사코드", "회사신용한도초과", "담보대출가능", "대주가능",
]

# ─────────────────────────── 코스닥 필드 스펙 ───────────────────────────
# KIS 공식 레포 stocks_info/kis_kosdaq_code_mst.py 의 field_specs/part2_columns 그대로.
_KOSDAQ_FIELD_SPECS: list[int] = [
    2, 1,
    4, 4, 4, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 9,
    5, 5, 1, 1, 1,
    2, 1, 1, 1, 2,
    2, 2, 3, 1, 3,
    12, 12, 8, 15, 21,
    2, 7, 1, 1, 1,
    1, 9, 9, 9, 5,
    9, 8, 9, 3, 1,
    1, 1,
]
_KOSDAQ_COLUMNS: list[str] = [
    "증권그룹구분코드", "시가총액 규모 구분 코드 유가",
    "지수업종 대분류 코드", "지수 업종 중분류 코드", "지수업종 소분류 코드", "벤처기업 여부 (Y/N)",
    "저유동성종목 여부", "KRX 종목 여부", "ETP 상품구분코드", "KRX100 종목 여부 (Y/N)",
    "KRX 자동차 여부", "KRX 반도체 여부", "KRX 바이오 여부", "KRX 은행 여부", "기업인수목적회사여부",
    "KRX 에너지 화학 여부", "KRX 철강 여부", "단기과열종목구분코드", "KRX 미디어 통신 여부",
    "KRX 건설 여부", "(코스닥)투자주의환기종목여부", "KRX 증권 구분", "KRX 선박 구분",
    "KRX섹터지수 보험여부", "KRX섹터지수 운송여부", "KOSDAQ150지수여부 (Y,N)", "주식 기준가",
    "정규 시장 매매 수량 단위", "시간외 시장 매매 수량 단위", "거래정지 여부", "정리매매 여부",
    "관리 종목 여부", "시장 경고 구분 코드", "시장 경고위험 예고 여부", "불성실 공시 여부",
    "우회 상장 여부", "락구분 코드", "액면가 변경 구분 코드", "증자 구분 코드", "증거금 비율",
    "신용주문 가능 여부", "신용기간", "전일 거래량", "주식 액면가", "주식 상장 일자", "상장 주수(천)",
    "자본금", "결산 월", "공모 가격", "우선주 구분 코드", "공매도과열종목여부", "이상급등종목여부",
    "KRX300 종목 여부 (Y/N)", "매출액", "영업이익", "경상이익", "단기순이익", "ROE(자기자본이익률)",
    "기준년월", "전일기준 시가총액 (억)", "그룹사 코드", "회사신용한도초과여부", "담보대출가능여부", "대주가능여부",
]


def _widths_and_columns(market: str) -> tuple[list[int], list[str]]:
    if market == "KOSPI":
        return _KOSPI_FIELD_SPECS, _KOSPI_COLUMNS
    if market == "KOSDAQ":
        return _KOSDAQ_FIELD_SPECS, _KOSDAQ_COLUMNS
    raise ValueError(f"지원하지 않는 시장: {market}")


def _parse_master(text: str, market: str) -> list[dict]:
    """종목마스터 원문(cp949 디코딩 완료 텍스트)을 파싱해 종목별 딕셔너리로 반환한다.

    각 행은 가변길이 head(단축코드 9자·표준코드 12자·한글명 나머지) + 고정폭
    tail(시장별 field_specs 합만큼)로 구성된다. tail 폭은 field_specs 총합으로
    직접 계산한다 — 하드코딩하면 원본 스크립트의 개행문자 포함 여부(228/222)와
    실제 콘텐츠 폭(227/221)이 어긋나는 off-by-one 함정이 있다.

    :raises SourceSchemaError: 행이 tail 폭보다 짧음, 데이터 행이 없음,
        part1/part2 파싱 후 컬럼·행 수가 기대와 다름(포맷이 바뀐 신호)
    """
    field_specs, columns = _widths_and_columns(market)
    tail = sum(field_specs)

    part1_rows: list[tuple[str, str, str]] = []
    part2_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line) <= tail:
            raise SourceSchemaError(
                "kis_master",
                f"{market} 마스터 행 길이가 예상보다 짧다(len={len(line)}, "
                f"기대 tail={tail}): {line[:60]!r}",
            )
        head = line[: len(line) - tail]
        part1_rows.append((head[0:9].strip(), head[9:21].strip(), head[21:].strip()))
        part2_lines.append(line[-tail:])

    if not part1_rows:
        raise SourceSchemaError("kis_master", f"{market} 마스터 파일에 데이터 행이 없다")

    part2_df = pd.read_fwf(
        io.StringIO("\n".join(part2_lines)), widths=field_specs, names=columns, dtype=str,
    ).fillna("")

    if len(part2_df.columns) != len(columns):
        raise SourceSchemaError(
            "kis_master",
            f"{market} part2 컬럼 수 불일치: {len(part2_df.columns)} != {len(columns)}",
        )
    if len(part2_df) != len(part1_rows):
        raise SourceSchemaError(
            "kis_master",
            f"{market} part1/part2 행 수 불일치: {len(part1_rows)} != {len(part2_df)}",
        )

    rows: list[dict] = []
    for (symbol, std_code, name), (_, part2_row) in zip(part1_rows, part2_df.iterrows()):
        if not symbol or not name:
            continue
        raw = part2_row.to_dict()
        raw["표준코드"] = std_code
        rows.append({"symbol": symbol.zfill(6), "name": name, "raw": raw})
    return rows


def _download_zip(market: str) -> bytes:
    # 쿨다운 검사는 호출 직전 한 곳(여기)에서만 한다 — 걸어두기만 하고 아무도 읽지
    # 않으면 죽은 코드이고, 다운로드 장애 시 매 호출이 네트워크 타임아웃을 새로 소진한다.
    # 이미 걸린 쿨다운을 검사만 하고 갱신하지는 않는다(여기서 note_failure 하면
    # 호출이 들어오는 한 쿨다운이 끝없이 연장된다).
    remaining = cooldown_remaining("kis_master")
    if remaining > 0:
        raise SourceUnavailableError("kis_master", f"쿨다운 중 — 다운로드 생략({remaining:.0f}초 남음)")

    url = _BASE_URL.format(name=_FILE_NAMES[market])
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as e:  # noqa: BLE001
        exc = classify_httpx("kis_master", e)
        note_failure(exc)
        logger.warning("KIS 종목마스터(%s) 다운로드 실패: %s", market, exc)
        raise exc from e


def _extract_mst_text(zip_bytes: bytes, market: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".mst")]
            if not names:
                raise SourceSchemaError(
                    "kis_master", f"{market} zip 안에 .mst 파일이 없다: {zf.namelist()}",
                )
            with zf.open(names[0]) as f:
                return f.read().decode("cp949")
    except zipfile.BadZipFile as e:
        raise SourceSchemaError("kis_master", f"{market} zip 파싱 실패: {e}") from e


def fetch_market_master(market: str) -> list[dict]:
    """market("KOSPI"|"KOSDAQ")의 종목마스터를 다운로드·파싱해 반환한다.

    각 항목: {"symbol": 6자리 코드, "name": 한글명, "raw": {필드명: 값, ...}}.

    :raises DataSourceError: 다운로드 실패(SourceUnavailableError 등) 또는
        파싱 실패(SourceSchemaError)
    """
    zip_bytes = _download_zip(market)
    text = _extract_mst_text(zip_bytes, market)
    return _parse_master(text, market)


async def snapshot_stock_master(db, trade_date: date | None = None) -> int:
    """KOSPI+KOSDAQ 종목마스터를 지정일(기본 오늘) 스냅샷으로 적재한다.

    한 시장이 실패해도 다른 시장은 저장한다(§48 부분 실패 관례) — 두 시장이 모두
    실패했을 때만 대표 예외를 올린다. 같은 날 재실행은 해당 시장 행을 지우고
    다시 넣어 덮어쓴다(멱등). flush 만 하고 commit 은 호출부 책임이다
    (krx_index.snapshot_sector_map 과 동일한 트랜잭션 경계).

    :raises DataSourceError: 두 시장 모두 실패했을 때, 원인 우선순위상 대표 예외
    """
    from sqlalchemy import delete

    from app.models import KisStockMasterSnapshot

    snap_date = trade_date or date.today()
    errors: list[DataSourceError] = []
    saved = 0

    for market in ("KOSPI", "KOSDAQ"):
        try:
            rows = fetch_market_master(market)
        except DataSourceError as e:
            errors.append(e)
            logger.warning("KIS 종목마스터(%s) 적재 실패 — 스킵: %s", market, e)
            continue

        await db.execute(
            delete(KisStockMasterSnapshot).where(
                KisStockMasterSnapshot.market == market,
                KisStockMasterSnapshot.trade_date == snap_date,
            )
        )
        db.add_all(
            [
                KisStockMasterSnapshot(
                    trade_date=snap_date, symbol=r["symbol"], market=market,
                    name=r["name"], raw=r["raw"],
                )
                for r in rows
            ]
        )
        saved += len(rows)

    if saved == 0 and errors:
        raise representative(errors)

    await db.flush()
    logger.info(
        "KIS 종목마스터 스냅샷 적재: trade_date=%s %d종목(성공 시장 %d/2)",
        snap_date, saved, 2 - len(errors),
    )
    return saved


async def latest_stock_master(db, symbol: str) -> dict | None:
    """symbol 의 가장 최근 종목마스터 스냅샷을 반환한다. 없으면 None.

    반환 딕셔너리: {"trade_date", "market", "name", **raw}.
    """
    from sqlalchemy import select

    from app.models import KisStockMasterSnapshot

    row = await db.scalar(
        select(KisStockMasterSnapshot)
        .where(KisStockMasterSnapshot.symbol == symbol)
        .order_by(KisStockMasterSnapshot.trade_date.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {"trade_date": row.trade_date, "market": row.market, "name": row.name, **row.raw}
