# API — REST · WebSocket · 인증

`backend/app/api/routes/` · FastAPI

## 엔드포인트 그룹

| 파일 | 경로 | 내용 |
|---|---|---|
| `auth.py` | `/api/auth/*` | 로그인·로그아웃. **브루트포스 방어**(IP 스로틀) |
| `strategies.py` | `/api/strategies/*` | 전략 CRUD·공유·좋아요·삭제 가드 |
| `backtests.py` | `/api/backtests/*` | 백테스트 실행(Celery)·조회 |
| `trading.py` | `/api/trading/*` | 주문·체결·포지션 조회 |
| `engine.py` | `/api/engine/*` | 엔진 제어(start/stop)·헬스 |
| `metrics.py` | `/api/metrics/*` | 종목·섹터 지표, 패닉, 취약도 |
| `screener.py` | `/api/screener/*` | 턴어라운드 스크리너 |
| `recommend.py` | `/api/recommend/*` | KOSPI200 스코어링 추천 |
| `tracking.py` | `/api/tracking/*` | 실측 NAV ↔ 백테스트 대조 |
| `fill_quality.py` | `/api/fill-quality/*` | 체결품질 M1/M2/M3 |
| `alerts.py` | `/api/alerts/*` | 알림 목록(커서 페이지네이션) |
| `news.py` `symbols.py` `kis.py` | | 뉴스·종목검색·KIS 직접조회 |
| `ws.py` | `/ws` | 실시간 이벤트 중계 |

## 인증

**서버측 세션**(JWT 아님). `app/core/session.py`

```
로그인 → create_session(user_id) → Redis  session:{sid} = user_id  (TTL=SESSION_TTL_MINUTES)
요청   → 쿠키 SESSION_COOKIE → get_session_user_id(sid) → 인증 성공 시 TTL 갱신(슬라이딩 만료)
```

- **X-Forwarded-For 를 신뢰하지 않는다.** 스푸핑으로 로그인 IP 스로틀을 우회할 수 있었다(§58).
  프록시 홉 수를 고려해 신뢰 가능한 클라이언트 IP 만 쓴다.
- WebSocket 도 같은 쿠키로 인증한다. 실패 시 `close(code=4401)`.

## WebSocket 규약 (`ws.py`)

```
연결 → 쿠키 인증 → accept → {"type":"connected","user_id":N}
     → Redis engine:events:{user_id} 구독 → 수신 payload 를 그대로 클라이언트로 relay
```

메시지 `type`: `order` · `execution` · `position` · `signal` · `alert`.
`alert` 에는 `severity`(warning|critical)·`code`·`message`(한국어)·`ts` 가 붙는다.

## 응답 계약

- **결측은 `Optional[...] = None`.** 0 sentinel 금지 — 실제 0과 구분이 안 된다(§50·§62·§64).
- 결측을 정렬할 땐 **맨 뒤로** 보낸다: `(x is None, ...)` 튜플 키.
- 결측을 필터링할 땐 **탈락**시킨다 — 결측은 더 엄격한 기준의 충족을 증명할 수 없다.
- NaN/inf → None 변환은 `app/services/_num.py`(`safe_float`·`is_nan`·`safe_bool`)를 쓴다.
  **주의: `is_nan` 은 `safe_float(v) is None` 이라 문자열이면 무조건 True 다.** 문자열 컬럼의
  결측 판정에 쓰면 안 된다(실제로 이 함정으로 시장 라벨이 전부 잘못 나온 적 있다).
- 빈 결과는 캐시하지 않는다(KRX 스로틀링·미발행을 굳히면 안 됨).
