"""경제 뉴스 수집·요약·종목 매핑 (docs/ROADMAP.md M3).

언론사 RSS 를 주기 수집(worker.ingest_news)해 news_articles 에 적재한다. 표시 방식은
본문 전재가 아닌 **요약+원문 링크**(출처 약관 준수). 세 단계로 나뉜다:

  1. 수집: FEEDS 의 RSS/Atom 을 파싱해 (제목, 링크, 설명, 발행시각)을 뽑는다.
  2. 요약: 현재는 RSS description 기반 추출 요약(summarize) — LLM 등으로 교체할 수
     있도록 이 함수 하나만 갈아끼우면 되게 격리한다.
  3. 종목 매핑: 제목+요약에 등장하는 종목명(신뢰 소스: app.services.symbols 카탈로그
     = krx_index.all_listed_stocks)을 찾아 news_article_symbols 에 역인덱스를 쌓는다.

의존성은 stdlib(xml.etree)+httpx 만 사용한다(feedparser 미도입).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NewsArticle, NewsArticleSymbol

logger = logging.getLogger(__name__)

# 수집 대상 피드. 공개 RSS 를 제공하는 국내 경제 매체 위주로 구성하며, 추가·교체는
# 이 목록만 고치면 된다. (URL 이 죽으면 해당 피드만 건너뛰고 알림 집계에 반영된다.)
FEEDS: list[dict[str, str]] = [
    {"source": "연합뉴스 경제", "url": "https://www.yna.co.kr/rss/economy.xml"},
    {"source": "매일경제 경제", "url": "https://www.mk.co.kr/rss/30100041/"},
    {"source": "매일경제 증권", "url": "https://www.mk.co.kr/rss/50200011/"},
    {"source": "한국경제 경제", "url": "https://www.hankyung.com/feed/economy"},
    {"source": "한국경제 증권", "url": "https://www.hankyung.com/feed/finance"},
]

# 피드당 수집 상한 — RSS 는 보통 최근 30~50건만 내려주지만 방어적으로 자른다.
_MAX_ITEMS_PER_FEED = 50
# 요약 최대 길이(자) — 목록에서 훑어보는 용도라 짧게 유지한다.
_SUMMARY_MAX_CHARS = 300
# 기사 보존 기간 — 시황 맥락 용도라 장기 보관 가치가 낮고, 무한 증식을 막는다.
RETENTION_DAYS = 60

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """HTML 태그·엔티티·연속 공백을 제거해 순수 텍스트로 만든다."""
    import html

    text = _TAG_RE.sub(" ", html.unescape(text or ""))
    return _WS_RE.sub(" ", text).strip()


def summarize(title: str, description: str) -> str | None:
    """기사 요약을 만든다 — 현재는 RSS description 추출 요약(교체 가능 지점).

    description 이 제목의 단순 반복이거나 비어 있으면 None(프론트는 제목만 표시).
    """
    text = strip_html(description)
    if not text or text == strip_html(title):
        return None
    if len(text) > _SUMMARY_MAX_CHARS:
        text = text[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


def _localname(tag: str) -> str:
    """네임스페이스가 붙은 XML 태그({ns}name)에서 로컬 이름만 뽑는다."""
    return tag.rsplit("}", 1)[-1]


def _parse_datetime(raw: str | None) -> datetime | None:
    """RSS pubDate(RFC 822)·Atom updated(ISO 8601)를 tz-aware UTC 로 파싱한다."""
    if not raw:
        return None
    raw = raw.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        # tz 누락 피드는 국내 매체 특성상 KST 로 간주한다.
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt.astimezone(timezone.utc)


def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0(channel/item) 또는 Atom(feed/entry) 문서를 공통 형태로 파싱한다.

    :return: [{title, url, description, published_at}] — 필수 필드(제목·링크)가 없는
        항목은 버린다. published_at 누락 시 None(호출부가 수집 시각으로 대체).
    """
    root = ElementTree.fromstring(xml_text)
    items: list[dict] = []

    for node in root.iter():
        if _localname(node.tag) not in ("item", "entry"):
            continue
        title = ""
        url = ""
        description = ""
        published: str | None = None
        for child in node:
            name = _localname(child.tag)
            text = (child.text or "").strip()
            if name == "title":
                title = strip_html(text)
            elif name == "link":
                # RSS 는 텍스트, Atom 은 href 속성.
                url = text or (child.get("href") or "").strip()
            elif name in ("description", "summary", "content"):
                description = description or text
            elif name in ("pubDate", "published", "updated", "date"):
                published = published or text
        if title and url:
            items.append(
                {
                    "title": title[:500],
                    "url": url[:1000],
                    "description": description,
                    "published_at": _parse_datetime(published),
                }
            )
        if len(items) >= _MAX_ITEMS_PER_FEED:
            break
    return items


_ASCII_NAME_RE_CACHE: dict[str, re.Pattern] = {}


def _ascii_name_matches(name: str, text: str) -> bool:
    """영문 종목명은 단어 경계를 요구한다 — "SK" 가 "RISK" 에 오검출되는 것 방지."""
    pat = _ASCII_NAME_RE_CACHE.get(name)
    if pat is None:
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])")
        _ASCII_NAME_RE_CACHE[name] = pat
    return bool(pat.search(text))


def match_symbols(text: str, name_map: dict[str, str]) -> list[str]:
    """텍스트(제목+요약)에 언급된 종목명을 찾아 종목코드 목록을 반환한다.

    규칙:
      - 한글명은 부분일치, 영문/숫자로만 된 이름은 단어 경계 일치(오검출 방지).
      - 1글자 이름은 제외(잡음).
      - 매칭된 이름이 더 긴 매칭 이름의 부분문자열이면 버린다 — "삼성전자우" 가 잡힌
        기사에서 "삼성전자" 를 함께 태그하지 않는다.
    """
    if not text:
        return []
    matched: dict[str, str] = {}  # name -> code
    for code, name in name_map.items():
        if len(name) < 2:
            continue
        if name.isascii():
            if _ascii_name_matches(name, text):
                matched[name] = code
        elif name in text:
            matched[name] = code

    names = list(matched)
    codes = [
        matched[name]
        for name in names
        if not any(other != name and name in other for other in names)
    ]
    return sorted(set(codes))


async def _load_name_map() -> dict[str, str]:
    """종목코드→한글명 매핑(blocking 카탈로그 빌드를 스레드풀로 우회)."""
    import asyncio

    from app.services import symbols

    return await asyncio.to_thread(symbols.get_name_map)


async def ingest_news(db: AsyncSession) -> dict:
    """FEEDS 를 수집해 신규 기사를 적재하고 종목 매핑을 쌓는다.

    url 유니크 기준으로 이미 있는 기사는 건너뛰므로 몇 번을 돌려도 안전하다(멱등).

    :return: {"feeds_ok", "feeds_failed", "fetched", "inserted"} — feeds_failed 가
        전체와 같으면 호출부(worker.ingest_news)가 수집 실패 알림을 발행한다.
    """
    name_map = await _load_name_map()
    now = datetime.now(timezone.utc)

    feeds_ok = 0
    failed: list[str] = []
    candidates: list[dict] = []  # {source, title, url, description, published_at}

    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={"User-Agent": "QuantFolio/1.0 (news aggregator; contact: admin)"},
    ) as client:
        for feed in FEEDS:
            try:
                resp = await client.get(feed["url"])
                resp.raise_for_status()
                items = parse_feed(resp.text)
                for item in items:
                    item["source"] = feed["source"]
                candidates.extend(items)
                feeds_ok += 1
            except Exception as e:  # noqa: BLE001 — 개별 피드 장애는 건너뛰고 집계만
                failed.append(feed["source"])
                logger.warning("뉴스 피드 수집 실패(%s): %s", feed["source"], e)

    # 같은 기사가 여러 피드에 실릴 수 있으므로 url 로 1차 중복 제거.
    by_url: dict[str, dict] = {}
    for item in candidates:
        by_url.setdefault(item["url"], item)

    inserted = 0
    if by_url:
        existing = set(
            (
                await db.execute(
                    select(NewsArticle.url).where(NewsArticle.url.in_(list(by_url)))
                )
            ).scalars()
        )
        for url, item in by_url.items():
            if url in existing:
                continue
            summary = summarize(item["title"], item["description"])
            article = NewsArticle(
                source=item["source"],
                title=item["title"],
                url=url,
                summary=summary,
                published_at=item["published_at"] or now,
            )
            db.add(article)
            await db.flush()  # article.id 확보
            for code in match_symbols(f"{item['title']} {summary or ''}", name_map):
                db.add(NewsArticleSymbol(article_id=article.id, symbol=code))
            inserted += 1
        await db.commit()

    result = {
        "feeds_ok": feeds_ok,
        "feeds_failed": failed,
        "fetched": len(by_url),
        "inserted": inserted,
    }
    logger.info("뉴스 수집 완료: %s", result)
    return result


async def purge_old_news(db: AsyncSession, days: int = RETENTION_DAYS) -> int:
    """보존 기간이 지난 기사를 삭제한다(매핑은 FK CASCADE 로 함께 정리)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(delete(NewsArticle).where(NewsArticle.published_at < cutoff))
    await db.commit()
    return result.rowcount or 0
