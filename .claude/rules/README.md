# QuantFolio 도메인 지식 인덱스

국내 주식(KRX) 퀀트 전략 **백테스팅** + 실시간 **자동매매** 웹 플랫폼.

이 폴더는 도메인별 지식을 나눠 담는다. 작업 전 아래 표에서 해당 문서 **하나만** 읽으면 된다.

| 무엇을 하려는가 | 읽을 문서 |
|---|---|
| 서비스가 어떻게 뜨고 서로 어떻게 통신하는지 | [architecture.md](architecture.md) |
| DB 테이블·관계·어느 도메인 소유인지 | [data-model.md](data-model.md) |
| 외부 데이터(KRX·DART·KIS·pykrx) 조회, 로컬 캐시 정책 | [market-data.md](market-data.md) |
| 백테스트 엔진, 팩터, 성과지표, 체결 모델 | [backtest.md](backtest.md) |
| 실시간 자동매매 데몬, 주문·리스크·정합 | [trading-engine.md](trading-engine.md) |
| REST/WebSocket 엔드포인트, 인증 | [api.md](api.md) |
| Next.js 화면·상태관리·표시 규약 | [frontend.md](frontend.md) |

## 이 폴더 밖의 기준 문서

| 문서 | 역할 |
|---|---|
| [`CLAUDE.md`](../../CLAUDE.md) | 프로젝트 최상위 규칙(항상 로드됨) |
| [`docs/CONVENTIONS.md`](../../docs/CONVENTIONS.md) | 코드 컨벤션 — **코드 작성·수정 시 필독** |
| [`docs/PRD.md`](../../docs/PRD.md) | 제품 정의 |
| [`docs/improvements.md`](../../docs/improvements.md) | 개선 이력·로드맵(§번호로 참조됨). 결정의 *이유*가 여기 있다 |
| [`docs/strategies.md`](../../docs/strategies.md) | 등록 전략 목록 |
| [`help/README.md`](../../help/README.md) | 백엔드 학습 가이드 |

## 전역 원칙 (도메인 무관)

- **주석의 근거는 측정 후에 적는다.** "안전하다"·"충분하다"류의 미측정 주장이 반복해서 문제를 일으켰다(타임아웃·루프·풀처럼 실패가 조용한 영역에서 특히).
- **결측은 sentinel(0)이 아니라 `None`/`null`.** 0은 실제 값과 구분이 안 된다. 프론트는 `lib/format.ts` 의 널 허용 헬퍼로 `"-"` 를 렌더한다.
- **실패의 종류를 뭉개지 않는다.** "조회 실패"·"데이터 없음"·"소스 미설정"·"미적재"는 서로 다른 사건이고 로그 레벨과 대응이 다르다.
- **`set` 을 순회해 순서 있는 결과를 만들지 않는다.** 문자열 해시는 프로세스마다 달라 재현성이 깨진다(§66).
