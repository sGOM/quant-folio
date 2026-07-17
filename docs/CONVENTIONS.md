# 코드 컨벤션 (QuantFolio)

유지보수성과 가독성을 지키기 위한 이 프로젝트의 코딩 규약.
실행 명령·운영 함정은 [`CLAUDE.md`](../CLAUDE.md), 제품 정의는 [`PRD.md`](PRD.md) 참고.
여기 규칙들은 기존 코드에서 실제로 지켜지고 있는 패턴을 명문화한 것이므로, 새 코드는 주변 코드와 이 문서 중 더 엄격한 쪽을 따른다.

## 0. 공통 원칙

- **언어**: 주석·docstring·커밋 메시지·문서·UI 문구·에러 메시지는 모두 **한국어**. 식별자(변수·함수·타입)는 영어.
- **주석은 "왜"를 쓴다.** 코드가 스스로 말하는 "무엇"을 반복하는 주석은 쓰지 않고, 제약·의도·함정만 남긴다.
  - 좋음: `# 변수명 risk_layer: engine.risk 모듈 import 를 가리지 않도록 한다`
  - 나쁨: `# risk_layer 를 가져온다`
- **금융 도메인 주석은 후하게.** 미래참조(look-ahead) 방지, 체결 규약(next_close), 수수료/세금 편도 적용, 레짐/패닉 오버레이 같은 수치 결정 로직은 모듈 docstring에 설계 원칙 단위로 문서화한다 (`backend/app/services/backtest/portfolio.py` 헤더가 표본).
- **낡은 주석은 버그다.** 리팩토링으로 함수명·구조가 바뀌면 이를 참조하는 주석/docstring도 같은 커밋에서 갱신한다.
- **파일 크기 신호**: 한 파일이 800줄을 넘고 서로 다른 관심사(상태·검증·표현 등)가 섞이기 시작하면 분해를 검토한다. 단, **수치 정확성이 검증된 백테스트 코어(상태기계)는 이득 없이 쪼개지 않는다** — 회귀 위험이 가독성 이득보다 크다.
- **삭제는 곧 개선.** 죽은 코드·사용 안 하는 export·중복 헬퍼는 발견 즉시 제거한다(호환 별칭이 필요하면 `_num.py`처럼 별칭 위치에 사유 주석).

## 1. 백엔드 (Python — FastAPI·engine·worker)

### 구조

- 레이어: `app/api`(라우터) → `app/services`(도메인 로직) → `app/models`(SQLAlchemy) / `app/schemas`(Pydantic v2). 라우터에 비즈니스 로직을 넣지 않는다.
- 실거래(`engine/`)와 백테스트(`app/services/backtest/`)가 공유하는 수치 로직(목표비중·점수)은 **한 곳에 두고 양쪽에서 재사용**한다. 복사·재구현 금지 — 백테스트/실거래 정합성이 제품 요구사항이다.
- 여러 모듈이 각자 정의하던 헬퍼는 공용 모듈로 승격한다 (예: NaN→None 변환은 `app/services/_num.py`).

### 스타일

- `from __future__ import annotations` + 내장 제네릭(`list[str]`, `X | None`) 타입힌트. 공개 함수는 시그니처에 타입 필수.
- 모듈 docstring: 첫 줄에 "이 모듈이 무엇인지" 한 문장, 이어서 설계 의도·불변식. 함수 docstring은 한 줄 평서문("~한다") 위주, 필요 시 인자 설명.
- **지역 변수가 import 한 모듈명을 가리지 않게 한다** (예: `risk = ...` 지역변수가 `from engine import risk` 를 shadowing → `risk_layer` 로 명명). 흔한 충돌 후보: `risk`, `metrics`, `json`, `time`.
- 비동기 일관성: 서비스 계층은 async SQLAlchemy 세션(`AsyncSessionLocal`)을 사용. 이벤트 루프를 막는 동기 I/O를 async 함수 안에 넣지 않는다.
- 돈·수량 계산은 규약을 주석으로 명시: 정수주 절사(floor), 수수료·세금·슬리피지의 편도/왕복 여부, 반올림 시점.
- 시크릿은 `secrets/*.txt` 마운트 + `app/core/config` 배선. 코드/`.env`에 하드코딩 금지.

### 테스트

- `pytest`, `asyncio_mode=auto` (async 테스트에 데코레이터 불필요). 위치는 `backend/tests/`.
- 수치 로직 변경 시 경계값(빈 universe, 단일 종목, NaN 구간, 레짐 전환일)을 반드시 테스트에 포함.
- 실행은 컨테이너 안에서: `docker compose exec web pytest`.

## 2. 프론트엔드 (Next.js 15 · React 19 · TS strict)

### 구조

- `app/`(라우트) · `components/`(공용 컴포넌트) · `lib/`(API 클라이언트·훅·순수 유틸). 페이지 컴포넌트는 데이터 패칭·레이아웃에 집중하고, 자체 상태를 가진 큰 블록(테이블·폼 섹션)은 컴포넌트로 추출한다.
- 대형 폼은 모듈 디렉터리로 분해하되 **공개 import 경로는 유지**한다 (표본: `components/strategy-form/` — `fields.tsx` 입력 프리미티브 / `validate.ts` 순수 검증 / 하위 폼 / 설명 카드, 본체 `StrategyForm.tsx`는 조립만).
- **검증·계산 로직은 React 상태와 분리해 순수 함수로** 둔다 → 그대로 vitest 단위 테스트 대상이 된다 (`strategy-form/validate.ts` + `__tests__/validate.test.ts`).
- 전략 메타데이터(라벨·기본값·설명)의 단일 소스는 `lib/strategy.ts`. 컴포넌트에 라벨 문자열을 흩뿌리지 않는다.

### 스타일

- TypeScript strict. `any` 금지 — 어쩔 수 없는 좁히기는 `as unknown as X` 로 한 곳에 국한하고 사유 주석.
- API 타입은 `lib/api.ts` 의 discriminated union(`StrategyConfig` 등)을 사용. 새 전략 유형·필드는 여기부터 추가한다.
- 클라이언트 컴포넌트에만 `"use client"`. 데이터 패칭은 TanStack Query, 실시간은 `useWebSocket`.
- 차트는 자체 SVG(`LineChart`) — 외부 차트 라이브러리 도입 금지.
- 숫자·통화·퍼센트 표시는 반드시 `lib/format.ts` 헬퍼 사용(부호 항상 표기, null → "-"). 손익 색상은 `text-profit`/`text-loss` 토큰 + `trendColor()` — raw 색상 클래스 직접 사용 금지, 색상에만 의존하지 말고 부호를 병행 표기.
- 스타일 상수는 공용을 재사용하되, **의도적 변형이면 왜 다른지 주석으로 명시** (표본: `RuleBuilder.tsx` 의 컴팩트 INPUT 주석).
- JSDoc 은 export 되는 함수·컴포넌트·비자명한 상수에 한국어 한 줄 이상. 예시 값을 포함하면 좋다 (`/** 원화 금액. 예: 1234567 → "1,234,567원" */`).

### 테스트

- vitest, 위치는 해당 모듈 옆 `__tests__/`. 테스트 이름은 한국어 평서문("이름이 비면 거부한다").
- 픽스처는 `defaultConfig()` 에서 파생한 오버라이드 헬퍼(`single(over)`, `rebal(over)`)로 만들어 기본값 변경에 강건하게 유지.
- 검증: `docker compose exec frontend npm run lint` → `npx vitest run` → `npm run build` 순서로 모두 통과해야 완료.

## 3. 네이밍

- Python: `snake_case` 함수/변수, `PascalCase` 클래스, 모듈 내부 전용은 `_` 접두. 상수는 `UPPER_SNAKE`.
- TypeScript: `camelCase` 함수/변수, `PascalCase` 컴포넌트/타입, 모듈 상수는 `UPPER_SNAKE`(`INPUT`, `FACTOR_META`).
- 이름은 **도메인 용어를 그대로**: `drift_band_pct`, `risk_layer`, `fill_mode`, `panic_overlay` 처럼 백엔드 스키마 필드명과 프론트 타입 필드명을 일치시킨다(직렬화 경계에서 이름 변환 금지).
- 부울은 긍정형 서술(`integer_shares`, `initial_fill_immediate`), 함수는 동사구(`compute_target_weights`, `validateStrategyForm`).

## 4. 변경 작업 절차 (Claude 포함 모든 기여자)

1. **읽고 나서 고친다**: 수정 전 해당 모듈의 docstring/헤더 주석을 읽어 설계 의도(특히 미래참조 방지·체결 규약)를 파악한다.
2. **리팩토링과 동작 변경을 한 커밋에 섞지 않는다.** 가독성 패스에서는 수치 결과가 바뀌면 안 된다(테스트가 그대로 통과해야 함).
3. 검증 게이트 — 아래 전부 통과 후 완료 보고:
   - 백엔드 변경: `docker compose exec web pytest` 전체 통과 + 변경 서비스 `docker compose restart <svc>` (web/engine/worker 는 핫리로드 없음)
   - 프론트 변경: lint → vitest → build 전체 통과
4. 커밋 메시지는 한국어, `type: 요약` 형식(`fix:`/`refactor:`/`test:`/`docs:`/`chore:`). 빌드 산출물(`tsconfig.tsbuildinfo` 등) 커밋 금지.
5. 새 전략 검증은 반드시 PIT(생존편향 제거) KOSPI200 유니버스로. 방어형 전략 성과 판정은 excess/IR 이 아닌 alpha/Sharpe 기준.
