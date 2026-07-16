# CLAUDE.md

국내 주식(KRX) 퀀트 전략 **백테스팅** + 실시간 **자동매매** 웹 플랫폼 (QuantFolio).
제품 정의는 [`docs/PRD.md`](docs/PRD.md), 백엔드 학습 가이드는 [`help/README.md`](help/README.md) 참고.

## 아키텍처 (한 줄 지도)

Docker Compose로 뜨는 별도 프로세스들. 서로 **Redis(pub/sub·큐·분산락)**로 통신.

| 서비스 | 정체 | 실행 명령 |
|--------|------|-----------|
| `web` | FastAPI REST + WebSocket (인증·CRUD·시세) | `uvicorn app.main:app` |
| `engine` | 24h 자동매매 데몬 (asyncio 이벤트루프) | `python -m engine.main` |
| `worker` | Celery (백테스트 등 비동기 작업) | `celery -A worker.celery_app.celery_app worker` |
| `frontend` | Next.js 15 (App Router, React 19) | `npm run dev` |
| `db` | PostgreSQL + TimescaleDB | — |
| `redis` | 세션·큐·pub/sub | — |
| `proxy` | Caddy | — |

- 백엔드 소스: `backend/app`(web) · `backend/engine`(엔진) · `backend/worker`(celery) · `backend/tests`
- 프론트: `frontend/app`(라우트) · `frontend/components` · `frontend/lib`
- DB 마이그레이션: `backend/alembic/versions`

## 자주 쓰는 명령

```bash
# 기동 / 재빌드
docker compose up -d --build
docker compose ps                       # 상태 확인
docker compose logs -f web              # 로그

# ⚠️ web/engine/worker는 코드 변경 시 자동 리로드 안 됨 → 재시작 필수
docker compose restart web
# 개발 중엔 docker-compose.override.yml 이 자동 병합되어 web 이 --reload 로 뜬다.
# (운영 배포는 docker compose -f docker-compose.yml up -d --build 로 override 제외)

# 백엔드 테스트 (컨테이너 안에서)
docker compose exec web pytest
docker compose exec web pytest tests/test_broker.py -k <name>

# DB 마이그레이션
docker compose exec web alembic upgrade head
docker compose exec web alembic revision --autogenerate -m "<msg>"

# 프론트 (컨테이너 안에서)
docker compose exec frontend npm run lint
docker compose exec frontend npm run build
```

## 필수 함정 (반복 실수 지점)

- **프론트 패키지 추가는 컨테이너 내부에 설치**해야 반영됨 (호스트 `npm install` X — 익명 볼륨 격리).
  → `docker compose exec frontend npm install <pkg>`
- **web/engine/worker는 핫리로드 없음.** 코드 고쳤으면 `docker compose restart <svc>`.
- 시크릿은 `.env`가 아니라 `secrets/*.txt` 파일 마운트. 새 시크릿은 compose secret + `app/core/config` 배선.
- `frontend/tsconfig.tsbuildinfo` 등 빌드 산출물은 커밋하지 말 것.
- 종목명 해석의 신뢰 소스는 `krx_index.all_listed_stocks`(KRX MDC). FDR/pykrx는 이 환경에서 불안정.
- KRX PIT 지수구성 조회는 KRX 로그인 필요(`KRX_ID/PW`가 `app.core.config`로 주입됨).

## 작업별 에이전트 라우팅

| 작업 | 에이전트 |
|------|----------|
| 백테스팅 코어·매매엔진·신호·리스크(수치 정확성) | `quant-engine` |
| FastAPI 엔드포인트·KIS 연동·Celery | `backend-fastapi` |
| Next.js 화면·차트·TanStack Query·WebSocket | `frontend-next` |
| UI/UX·디자인토큰·shadcn·접근성 | `ui-expert` |
| DB 스키마·마이그레이션·시계열 쿼리 | `db-architect` |
| 퀀트 전략 타당성·지표 수식·백테스트 방법론 자문 | `financial-expert` |
| 백엔드/프론트 코드 리뷰 | `review-fastapi` / `review-nextjs` |
| Git 커밋·브랜치·GitHub PR 생성/수정 | `pr-manager` |

앱을 실제로 띄워 검증·스크린샷은 `run-quantfolio` 스킬 사용.

## 전략 id 관리

등록 전략은 정수 id로 관리(현재 대표: **id=23** 균형 멀티팩터 — 저베타·순수 알파형, `alpha/Sharpe`로 판정).
신규 전략 검증은 반드시 **PIT(생존편향 제거) KOSPI200** 유니버스로. 손질된 풀은 성과가 붕괴함(생존편향).

## 컨벤션

- 커밋 메시지·주석·문서: **한국어**.
- 백엔드: FastAPI + Pydantic v2 + SQLAlchemy 2 (async). 테스트는 `pytest` (`asyncio_mode=auto`).
- 프론트: TypeScript strict, 자체 SVG 차트(외부 차트 라이브러리 미사용).
