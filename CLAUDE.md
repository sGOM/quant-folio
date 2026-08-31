# CLAUDE.md

국내 주식(KRX) 퀀트 전략 **백테스팅** + 실시간 **자동매매** 웹 플랫폼 (QuantFolio).
제품 정의는 [`docs/PRD.md`](docs/PRD.md), 백엔드 학습 가이드는 [`help/README.md`](help/README.md) 참고.
**코드 작성·수정 시 [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md)(코드 컨벤션)를 따를 것.**

## 도메인 지식 — `.claude/rules/`

작업 전 아래 표에서 **해당 문서 하나만** 읽는다(전부 읽지 말 것).
인덱스: [`.claude/rules/README.md`](.claude/rules/README.md)

| 작업 영역 | 문서 |
|---|---|
| 서비스 구성·Redis 통신 규약·배치 스케줄·기동 함정 | `.claude/rules/architecture.md` |
| DB 테이블·관계·삭제 정책 | `.claude/rules/data-model.md` (→ `data-model/trading.md`, `data-model/market-store.md`) |
| 외부 데이터 조회·캐시 정책·팩터 | `.claude/rules/market-data.md` |
| 전략 설정 필드·블록 조립(`RebalanceConfig` 등) | `.claude/rules/strategy-config.md` |
| 백테스트 엔진·성과지표·전략 판정 기준 | `.claude/rules/backtest.md` |
| 신규 팩터·전략 검증 프로토콜·검증 스크립트 | `.claude/rules/validation-workflow.md` |
| 실시간 매매 엔진·주문 멱등·리스크 | `.claude/rules/trading-engine.md` |
| REST/WS 엔드포인트·인증·응답 계약 | `.claude/rules/api.md` |
| 알림 체계·`code` 레지스트리 | `.claude/rules/alerts.md` |
| Next.js 화면·표시 규약 | `.claude/rules/frontend.md` |

## 아키텍처 (한 줄 지도)

Docker Compose로 뜨는 별도 프로세스들. 서로 **Redis(pub/sub·큐·분산락)**로 통신.

| 서비스 | 정체 | 실행 명령 |
|--------|------|-----------|
| `web` | FastAPI REST + WebSocket (인증·CRUD·시세) | `uvicorn app.main:app` |
| `engine` | 24h 자동매매 데몬 (asyncio 이벤트루프) | `python -m engine.main` |
| `worker` | Celery (백테스트 등 비동기 작업 + beat 스케줄) | `celery -A worker.celery_app.celery_app worker -B` |
| `frontend` | Next.js 15 (App Router, React 19) | `npm run dev` |
| `db` | PostgreSQL + TimescaleDB | — |
| `redis` | 세션·큐·pub/sub | — |
| `proxy` | Caddy | — |

## 자주 쓰는 명령

```bash
# 기동 / 재빌드
docker compose up -d --build
docker compose ps                       # 상태 확인
docker compose logs -f web              # 로그

# ⚠️ web/engine/worker 재시작 필수(핫리로드 없음 — 아래 "필수 함정" 참고)
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
- 확정 과거 데이터(펀더멘털·시총·OHLCV·PIT구성·DART재무)는 Postgres 에 영구 저장돼
  로컬 우선으로 읽힌다. 조회 계약은 `app/services/data/store/frame.py`, 강제 재적재는
  각 리포지토리의 `delete_*` 후 `external_fetches` 행 삭제.

앱을 실제로 띄워 검증·스크린샷은 `run-quantfolio` 스킬 사용.
작업별 에이전트 선택은 `.claude/agents/*.md` 의 description 을 따른다(매 세션 자동 주입됨).

## 전략 id 관리

등록 전략은 정수 id로 관리(현재 대표: **id=23** 균형 멀티팩터 — 저베타·순수 알파형, `alpha/Sharpe`로 판정).
신규 전략 검증은 반드시 **PIT(생존편향 제거) KOSPI200** 유니버스로. 손질된 풀은 성과가 붕괴함(생존편향).

## 문서 유지

- **무효화·기독(旣讀) 문서만 그 자리에서 갱신**: 작업이 어떤 문서의 서술을 무효화하거나
  작업 중 이미 그 문서를 읽었다면, 같은 세션에서 그 문서만 고친다(발견 비용이 이미 치러져
  저렴·정확). 로드맵(`docs/improvements.md`) 항목의 완료 반영도 이 규칙을 따른다.
- **전 문서 정합 점검·신규 발굴은 batch**: 관련 있을 수 있는 문서를 두루 훑는 정합 점검이나
  코드베이스 재점검으로 새 개선안을 뽑는 작업은 탐색 비용이 크므로, 매번 하지 말고 몇
  마일스톤마다 한 번 몰아서 한다.
