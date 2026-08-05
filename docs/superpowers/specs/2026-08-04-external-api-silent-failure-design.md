# 외부 데이터 소스의 조용한 실패 제거 — 설계

작성일: 2026-08-04
관련: `docs/improvements.md` §44-1, 커밋 `9b8c528`(KRX 로그인 쿨다운), `f309184`(빈 후보풀 에러 로그)

## 1. 문제

`app/services/data/` 의 세 모듈(`krx_index`·`opendart`·`kofia`)은 외부 호출이 실패해도
예외를 던지지 않고 빈 값(`[]`/`{}`/`None`)을 반환한다. 세 모듈 합쳐 49곳이다.

문제는 실패가 감춰진다는 것 자체가 아니라, **실패한 빈 값과 정상적으로 빈 값이 같은
값으로 표현된다**는 점이다. 호출자는 둘을 구분할 방법이 없다.

§44-1 에서 이 구조가 실제 사고를 냈다. KRX 가 로그인을 차단하자 모든 PIT 조회가
0종목을 반환했고, 백테스트는 **빈 패널 위에서 '성공'하며 무의미한 수치를 냈다**.
그 수치가 그대로 보고됐다면 잘못된 결론이 로드맵에 박혔을 것이다.

`opendart._get` 은 더 나쁘다. 미설정·네트워크 실패·에러 status·무자료의 **네 가지가
전부 `None`** 이다. 특히 일일 20,000건 한도 초과(status `020`)가 "조회된 데이터 없음"
(`013`)과 구분되지 않는다. 한도를 소진하면 전 종목이 조용히 "재무 정보 없음"이 된다.

## 2. 목표와 비목표

**목표**: 외부 소스 실패가 호출자에게 반드시 전달되게 한다. 실패를 값이 아니라 제어
흐름으로 만들어, 호출자가 무시할 수 없게 한다.

**비목표(이번 범위 밖)**:
- 자동 백오프 재시도 — 예외에 `retryable` 속성만 깔고 로직은 다음 작업으로.
  목표는 조용한 실패 제거이지 복원력 강화가 아니다. 재시도를 끌어들이면 전송 계층이
  다시 커진다.
- 영속 캐시(호출량 감축), 장애 관측성(연속 실패 알림), 타임아웃 예산 일관화.
  모두 별개 작업으로 검토됐고 이번엔 다루지 않는다.
- 백테스트 응답 스키마 변경(중립화 축 생략을 결과 메타에 표시).

## 3. 경계 정의

설계의 핵심. 세 가지를 구분한다.

| 구분 | 의미 | 처리 |
|---|---|---|
| **실패** | 소스가 답을 못 줬다 | 예외 raise |
| **데이터 없음** | 소스가 "없다"고 답했다 | 정상 빈 값 |
| **미설정** | 애초에 안 물어봤다 | 현행 유지(예외 아님) + 용도별 preflight |

이 경계가 관념이 아니라 **코드로 판별 가능**하다는 근거가 있다. KRX 차단 시 응답은
JSON 이 아닌 HTML 이라 `resp.json()` 이 예외를 던진다(`krx_index.py:146`). 반면 진짜
휴장일은 정상 JSON + 빈 `output` 이라 예외가 없다. 두 상황이 실제로 다른 경로를 탄다.

### 데이터 없음의 위험과 완화

"없다"는 답이 항상 진실은 아니다. DART `013` 은 "아직 공시 안 함"뿐 아니라
**`corp_code` 가 틀렸을 때도** 온다. KRX 에 미래 날짜를 주면 정상 빈 응답이 온다.
즉 이 경계는 조용한 실패의 새 은신처가 될 수 있다.

완화 장치는 집계 계층의 "전량 실패 → raise" 규칙이다(§5). 개별 종목이 "정상적으로
없음"인 건 통과시키되, **전 종목이 그러면 데이터가 아니라 사고**로 본다. 두 경계가
서로의 구멍을 덮으므로 따로 떼면 둘 다 약해진다.

### 미설정을 예외로 만들지 않는 이유와 그 구멍

`opendart.is_enabled()` 는 이미 API 응답(`opendart_enabled`)까지 배선돼 있어 사용자에게
"재무 팩터 없이 돌았다"가 표시된다. 이건 조용한 실패가 아니라 드러난 저하다.

그러나 `KRX_ID/PW` 가 없으면 `index_members` 가 `[]` 를 반환하고(`krx_index.py:133-135`),
백테스트는 빈 패널 위에서 '성공'한다 — **§44-1 과 결과가 글자 그대로 동일하다.**
"미설정은 실패가 아니다"는 원칙은 옳지만, PIT 유니버스처럼 없으면 결과가 무의미해지는
필수 입력에는 맞지 않는다.

**해법**: 필수/선택을 *소스* 가 아니라 *용도* 로 가른다. `_session()` 은 미설정에
`None` 을 유지하되, PIT 백테스트 진입점에서 `require_krx_auth()` 로 사전 검사한다.
개발환경에서 앱은 그대로 뜨고 무의미한 실행만 시작 전에 막힌다.

## 4. 예외 계층 — 원인별

소스가 아니라 **원인** 으로 나눈다. 호출자가 궁금한 건 "KRX 냐 DART 냐"가 아니라
"재시도해도 되나, 사람이 고쳐야 하나"이기 때문이다. 두 축을 다 타입으로 만들면
조합만 15개가 된다. 소스는 속성으로 싣는다.

**새 파일 `app/services/data/errors.py`** (기존 `broker/base.py::BrokerError` 계층과 대칭):

```python
class DataSourceError(Exception):
    source: str                    # "krx" | "dart" | "kofia"
    retryable: bool
    retry_after: float | None

class SourceAuthError(DataSourceError):         # 인증·권한·차단 — 사람이 고쳐야
class SourceQuotaError(DataSourceError):        # 한도 초과 — 창구 리셋까지 대기
class SourceUnavailableError(DataSourceError):  # 일시 장애 — 재시도 유효
class SourceSchemaError(DataSourceError):       # 응답이 계약과 다름 — 파서 수정 필요
class SourceRequestError(DataSourceError):      # 우리가 잘못 요청 — 버그
```

### 판별 매핑

| 상황 | 판별 근거 | 예외 |
|---|---|---|
| DART `010`/`011`/`012` | status | Auth |
| DART `020`/`021` | status | Quota |
| DART `100`/`101` | status | Request |
| DART `800`(점검)·`900`·미지 코드 | status | Unavailable (보수적) |
| DART `013` | status | **예외 아님** (`None`) |
| KRX HTTP 200 + 비JSON | 파싱 실패 | Auth (차단) |
| KRX 로그인 실패·빈 세션·쿨다운 중 | 로컬 상태 | Auth |
| KRX/KOFIA 기대 키 부재(`output`·`OutBlock_1`·`ds1`) | 파싱 | Schema |
| 공통: 타임아웃·연결오류·5xx | 예외 / status_code | Unavailable |
| 공통: 4xx | status_code | Request |

DART status 코드 목록은 구현 시 OpenDART 공식 문서로 재확인한다. 미지 코드는
Unavailable 로 보수적 분류한다(재시도 가능 쪽).

**한계 — 명시해 둔다**: KRX 는 에러 코드 체계가 없어 "HTTP 200 인데 JSON 이 아니면
차단"이라는 휴리스틱에 기댄다. §44-1 에서 실제 관측한 동작이지만, KRX 가 다른 형태로
막으면 Unavailable 로 오분류된다. 그래서 Unavailable 에도 짧은 쿨다운(60s)을 걸어
오분류 시에도 재시도 폭주가 나지 않게 한다.

### 원인별 쿨다운

원인을 나누는 값은 이름이 아니라 정책이 달라진다는 데 있다.

| 원인 | 쿨다운 | 근거 |
|---|---|---|
| Auth | 300s (현행 `_SESSION_FAIL_COOLDOWN` 유지) | 재시도해도 안 풀리고 차단만 악화 |
| Quota | DART 일일 한도면 다음 자정까지 | 그 전엔 확정적으로 실패 |
| Unavailable | 60s | 회복 가능. 오분류 대비 |
| Schema / Request | 없음 — 즉시 raise + ERROR 로그 | 코드 수정 신호. 쿨다운은 오히려 은폐 |

이는 KRX 에 이미 있는 쿨다운 메커니즘의 확장이지 신규 설계가 아니다.

## 5. 계층 규약

| 계층 | 책임 | 실패 시 |
|---|---|---|
| 전송 | HTTP 1회 왕복, 응답 형태 검증 | 무조건 raise |
| 집계 | 종목·기간 루프, 캐시 | 성공 0건이면 raise, 아니면 부분 결과 + 실패 로그 |
| 호출자 | 백테스트·팩터·추천 | 잡지 않음(실행 실패로 전파) |

### "성공 0건"의 정의

**성공 = 예외 없이 응답을 받음(무자료 포함)** 이지, 데이터를 얻음이 아니다.

이 구분이 중요한 이유: `metrics_by_symbol` 은 `announcement_lagged_year` 로 과거 시점
보고서를 조회하는데, 오래된 구간일수록 DART 에 자료가 없는 종목 비율이 높다. "획득
0건 = 실패"로 잡으면 정상 응답만 오는 구간에서 **정상 백테스트가 죽는다.** "응답 수신"
으로 잡으면 그 구간은 통과하고, 한도 초과나 차단은 응답 자체가 실패하므로 여전히 잡힌다.

임계는 비율이 아니라 **전량**이다(성공 0 & 실패 ≥ 1). 튜닝 파라미터를 만들지 않으면서
§44-1(사실상 전량 실패)을 정확히 잡는다.

## 6. 모듈별 변경

### 전송 계층

| 위치 | 현행 | 변경 |
|---|---|---|
| `krx_index` POST 5곳 (`index_members`·`all_listed_stocks`·`market_caps`·`sector_map`·`etf_leverage_exposure`) | `except → rows=[]` | 원인별 raise |
| `krx_index._session` | 미설정·실패 모두 `None` | 미설정 `None` / 실패·쿨다운 `SourceAuthError` |
| `opendart._get` | 미설정·실패·에러status·무자료 모두 `None` | 무자료(`013`)·미설정만 `None`, 나머지 원인별 raise |
| `opendart.corp_code_map` | 실패 `None`, 빈 매핑 `{}` | 실패 raise, **빈 매핑도** `SourceSchemaError` |
| `kofia._rows` | `except → []` | 원인별 raise (호출자 없어 무위험) |

### 집계 계층

- `index_members`·`market_caps`·`sector_map` (7일 소급 루프): 시도 중 **정상 응답이 한 번도
  없으면** raise. 정상 응답이 있었는데 전부 빈 `output` 이면 진짜 휴장/미상장 → 현행대로 빈 값.
- `metrics_by_symbol`·`pead_sue_by_symbol` (종목 루프): 종목별 실패를 세고 성공 0 & 실패 ≥1
  이면 raise. 전 종목 무자료라 `out` 이 비는 건 실패가 아니다.
- `etf_leverage_exposure`·`fetch_credit_balance`·`fetch_market_funds`: 단일 조회라 전송
  예외가 그대로 전파.

원인이 섞이면 대표 예외는 **Auth > Quota > Request > Schema > Unavailable** 우선순위로
고르고, 메시지에 원인별 건수를 담는다.

**캐시 정책은 손대지 않는다.** 세 모듈 모두 이미 "성공만 캐시"라 실패가 고착되지
않는다(`krx_index.py:151-154` 주석이 그 이유를 이미 기록).

### preflight

`errors.py` 에 `require_krx_auth()` 를 두고 PIT 유니버스 백테스트 진입점
(`backtests.py` 의 PIT 경로)과 검증 스크립트에서 시작 전에 호출한다. 인증이 없으면
19개월치를 다 돌기 전에 즉시 막힌다.

## 7. 호출자 처리

기준은 하나 — **그 데이터가 없으면 결과 수치가 오염되는가.**

### 전파(잡지 않음)

| 호출자 | 이유 |
|---|---|
| `backtests.py:319` `_build_pit_pool` | §44-1 사고 지점. + `require_krx_auth()` preflight |
| `recommend.py:132`, `screener.py:109` | 재무 없는 추천/스크리닝은 잘못된 추천. API 는 503 매핑(아래) |
| 검증 스크립트 | 이미 `f309184` 에서 빈 패널 중단 적용 |

전파되는 예외가 API 경계에 닿으면 지금은 `main.py:57` 의 전역 `Exception` 핸들러가
받아 500 이 된다. 외부 소스 장애는 서버 버그가 아니므로 `DataSourceError` 전용
핸들러를 추가해 **503 + `source`·원인** 을 응답한다(전역 핸들러보다 먼저 매칭됨).

### 명시적 저하(잡되 ERROR 로그)

| 호출자 | 저하 내용 |
|---|---|
| `backtests.py:174/184`, `factors.py:679/693` | 중립화 축 생략. §20 에 이미 설계된 저하 |
| `symbols.py:125`, `news.py` | 종목명 해석 실패. 매매는 계속돼야 함 |
| `ingest.py:33` | 수집 대상에서 그 부분만 제외 |
| `portfolio.py:794` | 섹터 노출 리포트 생략 |

### 부수 효과 — bare except 제거

명시적 저하 그룹은 지금 전부 `except Exception` 이다(`backtests.py:176`,
`symbols.py:132`, `ingest.py:36`). 외부 장애뿐 아니라 `TypeError` 같은 **우리 쪽 버그까지
삼키고 있다.** `except DataSourceError` 로 좁히면 그 은신처가 같이 사라진다.
이번 작업에서 가장 조용했던 실패일 수 있다.

## 8. 테스트

기존 `tests/test_krx_index.py::TestSessionFailureCooldown` 의 가짜 세션 주입 패턴을 재사용한다.

**원인 판별(전송 계층)**
- KRX HTTP 200 + HTML 본문 → `SourceAuthError`
- KRX 타임아웃 → `SourceUnavailableError`
- KRX 정상 JSON + `output` 키 부재 → `SourceSchemaError`
- KRX 정상 JSON + 빈 `output` → 예외 아님, 빈 값
- DART `020` → `SourceQuotaError`, `011` → `SourceAuthError`, `100` → `SourceRequestError`
- DART `013` → 예외 아님(`None`)
- KOFIA `ds1` 부재 → `SourceSchemaError`

**집계 계층**
- 7일 루프 전량 실패 → raise
- 중간 실패 후 성공 → 정상 반환(예외 없음)
- 정상 응답인데 전부 빈 값 → 빈 값 반환 **(핵심 회귀 방지)**
- 종목 전량 무자료 → `{}` 반환, 전량 실패 → raise
- 원인 혼재 시 대표 예외 우선순위

**쿨다운**
- Auth 300s / Unavailable 60s / Schema 쿨다운 없음
- 쿨다운 중 재시도 생략, 만료 후 재시도(기존 3건 유지)

**preflight**
- 미인증 상태에서 PIT 백테스트 즉시 차단

**회귀**
- `except DataSourceError` 로 좁힌 뒤 `TypeError` 가 삼켜지지 않고 전파되는지

대략 25건 내외.

## 9. 남는 위험

이 설계는 **KRX 인증이 정상일 때를 전제**한다. 현재 KRX 는 §44-1 로 차단된 상태이고
해제 시점을 알 수 없다(`improvements.md:1044`). 즉 전송 계층 변경을 **실제 KRX 응답으로
검증할 수 없고**, 가짜 세션 기반 단위 테스트까지만 확인 가능하다. 차단이 풀린 뒤
통합 확인이 한 번 더 필요하다.

부차적 위험: CLAUDE.md 가 이미 "FDR/pykrx 는 이 환경에서 불안정"이라 기록하고 있다.
그 불안정성이 지금은 빈 결과로 흡수되다가 앞으로는 백테스트 실패로 표면화된다.
정직해지는 것이지만 체감은 퇴행이고, 자동 재시도(비목표)가 없는 동안은 실패율이
눈에 띄게 오를 수 있다. 이것이 §2 에서 재시도를 "다음 작업"으로 명시한 이유다.
