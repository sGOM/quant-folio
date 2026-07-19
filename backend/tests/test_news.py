"""뉴스 수집 서비스(app/services/news.py) 순수 로직 검증 — 파싱·요약·종목 매핑.

네트워크·DB 없이 RSS/Atom 파싱, 추출 요약, 종목명 매칭 규칙(부분문자열 억제·영문
단어 경계)을 검증한다.
"""
from __future__ import annotations

from datetime import timezone

from app.services.news import (
    _parse_datetime,
    match_symbols,
    parse_feed,
    strip_html,
    summarize,
)

_RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>테스트 경제 뉴스</title>
    <item>
      <title>삼성전자, 2분기 &lt;b&gt;깜짝&lt;/b&gt; 실적</title>
      <link>https://example.com/news/1</link>
      <description>&lt;p&gt;삼성전자가 시장 예상을 웃도는 실적을 발표했다.&lt;/p&gt;</description>
      <pubDate>Fri, 18 Jul 2026 09:30:00 +0900</pubDate>
    </item>
    <item>
      <title>제목만 있고 링크 없는 항목</title>
    </item>
    <item>
      <title>코스피 상승 마감</title>
      <link>https://example.com/news/2</link>
      <description>코스피 상승 마감</description>
      <pubDate>Fri, 18 Jul 2026 16:00:00 +0900</pubDate>
    </item>
  </channel>
</rss>"""

_ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>테스트 Atom</title>
  <entry>
    <title>금리 동결 전망</title>
    <link href="https://example.com/atom/1"/>
    <summary>한국은행이 기준금리를 동결할 전망이다.</summary>
    <updated>2026-07-18T10:00:00+09:00</updated>
  </entry>
</feed>"""


def test_parse_feed_rss():
    items = parse_feed(_RSS_SAMPLE)
    # 링크 없는 항목은 버려진다.
    assert len(items) == 2
    first = items[0]
    # 제목의 HTML 태그·엔티티가 제거된다.
    assert first["title"] == "삼성전자, 2분기 깜짝 실적"
    assert first["url"] == "https://example.com/news/1"
    assert first["published_at"].tzinfo is not None
    assert first["published_at"].astimezone(timezone.utc).hour == 0  # 09:30 KST → 00:30 UTC


def test_parse_feed_atom():
    items = parse_feed(_ATOM_SAMPLE)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/atom/1"
    assert "동결" in items[0]["description"]
    assert items[0]["published_at"] is not None


def test_parse_datetime_naive_assumes_kst():
    dt = _parse_datetime("2026-07-18T09:00:00")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 0  # 09:00 KST → 00:00 UTC


def test_summarize_strips_html_and_truncates():
    assert strip_html("<p>안녕 &amp; 세계</p>") == "안녕 & 세계"
    # 제목의 단순 반복이면 요약 없음.
    assert summarize("코스피 상승 마감", "코스피 상승 마감") is None
    long = "가" * 500
    s = summarize("제목", long)
    assert s is not None and len(s) <= 300 and s.endswith("…")


_NAME_MAP = {
    "005930": "삼성전자",
    "005935": "삼성전자우",
    "000660": "SK하이닉스",
    "034730": "SK",
    "035420": "NAVER",
}


def test_match_symbols_basic():
    codes = match_symbols("삼성전자가 실적을 발표했다", _NAME_MAP)
    assert codes == ["005930"]


def test_match_symbols_longest_name_wins():
    # "삼성전자우" 언급 기사에 "삼성전자"(부분문자열)를 함께 태그하지 않는다.
    codes = match_symbols("삼성전자우 배당 확대", _NAME_MAP)
    assert codes == ["005935"]
    # "SK하이닉스" 기사에 "SK" 를 함께 태그하지 않는다.
    codes = match_symbols("SK하이닉스 HBM 수주", _NAME_MAP)
    assert codes == ["000660"]


def test_match_symbols_ascii_word_boundary():
    # 영문명은 단어 경계 필요 — "RISK" 안의 "SK" 는 오검출하지 않는다.
    assert match_symbols("RISK management update", _NAME_MAP) == []
    # 독립적으로 언급되면 매칭된다.
    assert match_symbols("SK 그룹 지배구조 개편", _NAME_MAP) == ["034730"]
    assert match_symbols("NAVER 2분기 호실적", _NAME_MAP) == ["035420"]


def test_match_symbols_empty_text():
    assert match_symbols("", _NAME_MAP) == []
