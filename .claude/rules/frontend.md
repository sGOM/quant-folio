---
paths:
  - "frontend/**"
---

# 프론트엔드

`frontend/` — Next.js 15 (App Router) · React 19 · TanStack Query · Tailwind/shadcn

## 화면

| 경로 | 내용 |
|---|---|
| `app/dashboard` | 요약 |
| `app/strategies` | 전략 목록·빌더·상세(`[id]`)·공유(`shared`) |
| `app/monitor` | 실시간 모니터링(WS)·워치리스트·체결품질 |
| `app/metrics` | 종목/섹터 지표 테이블·패닉 오버레이 |
| `app/screener` `app/recommend` | 스크리너·추천 |
| `app/news` `app/settings` `app/login` | |

## 공용

| 파일 | 역할 |
|---|---|
| `lib/api.ts` | **백엔드 응답 타입의 단일 정의**(1500여 줄). 백엔드 스키마를 바꾸면 여기부터 맞춘다 |
| `lib/format.ts` | 표시 헬퍼 |
| `lib/useWebSocket.ts` | WS 연결 — **중복 연결 주의**(§40에서 실제로 터졌다) |
| `lib/useAuth.tsx` `components/RequireAuth.tsx` | 인증 게이트 |
| `lib/strategy.ts` `components/strategy-form/` `RuleBuilder.tsx` | 전략 빌더·검증 |
| `components/LineChart.tsx` | **자체 SVG 차트**(차트 라이브러리 의존 없음) |
| `components/AlertCenter.tsx` | 알림 — `useInfiniteQuery` 로 이전 이력 로드 |
| `lib/glossary.ts` `components/GlossaryDrawer.tsx` | 용어 설명 |

## 표시 규약 — 널 허용 헬퍼를 쓴다

백엔드가 결측을 `null` 로 내려주므로, **인라인 삼항식을 새로 쓰지 말고 `lib/format.ts` 의
헬퍼를 쓴다**(§63·§64 에서 반복 정리했다).

| 헬퍼 | 용도 | null |
|---|---|---|
| `fmtPct(v, digits)` | 소수 비율 → 부호 포함 % | `"-"` |
| `fmtNum(v, digits)` | 고정 소수 자릿수 | `"-"` |
| `fmtKRW(v, withUnit)` | 원화 금액 | `"-"` |
| `fmtAmt(v)` | 원화 축약(조/억/만) | `"-"` |
| `pctColor(v)` | 손익 색상 클래스 | muted |
| `fmtTrackingError(v)` (`lib/tracking.ts`) | 연율 트래킹에러 | `"-"` |

정렬은 **null 을 하단으로** (`sortRows` 가 이미 처리한다).

## 주의

- **패키지 설치는 컨테이너 내부에서.** `docker compose exec frontend npm install <pkg>`
- `app/*/page.tsx` 에 **named export 를 두면 Next.js 15 페이지 타입 계약이 깨져 빌드가 실패한다**.
- `localStorage` 접근은 try/catch — 예외가 페이지 전체를 죽인 적 있다(워치리스트).
- 검증: `npm run lint` → `npm run test`(vitest) → `npm run build` 를 모두 통과시킨다.
