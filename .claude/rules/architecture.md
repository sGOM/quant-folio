---
paths:
  - "docker-compose*.yml"
  - "Caddyfile"
  - "backend/Dockerfile"
  - "frontend/Dockerfile"
  - "backend/worker/celery_app.py"
  - "backend/app/core/channels.py"
  - ".github/workflows/**"
---

# 아키텍처 — 프로세스와 통신

Docker Compose 로 뜨는 **별도 프로세스**들이 **Redis**(pub/sub·큐·분산락)로 통신한다.
서로 함수를 직접 호출하지 않는다.

서비스 목록·실행 명령은 `CLAUDE.md` 의 "아키텍처(한 줄 지도)" 표를 본다(상시 로드됨).

## Redis 통신 규약

**단일 소스: `app/core/channels.py`.** web 과 engine 이 이 모듈을 공유해 규약을 맞춘다.
채널명·키 프리픽스를 문자열 리터럴로 따로 들고 있으면 안 된다.

| 키/채널 | 방향 | 용도 |
|---|---|---|
| `engine:control` | web → engine | 전략 ON/OFF `{"action":"start"\|"stop","strategy_id":int}` |
| `engine:active_strategies` (SET) | web/engine | 운용 중 전략 ID — 엔진 재기동 복구용 |
| `engine:events` / `engine:events:{user_id}` | engine → web | 주문·체결·포지션·신호·알림 → WS 푸시 |
| `engine:heartbeat` (TTL) | engine → web | 엔진 생존 신호 |
| `engine:health:{strategy_id}` | engine → web | 러너별 헬스(연속 실패·마지막 성공) |
| `session:{sid}` | web | 로그인 세션 → user_id, 슬라이딩 TTL |
| `lock:order:{idempotency_key}` | engine | 주문 멱등 분산락 |
| `lock:position:{user_id}:{symbol}` | engine | 종목 포지션 직렬화(TOCTOU 방지) |

`FAILURE_ALERT_THRESHOLD = 3` — 러너 연속 실패가 이 횟수면 알림 1회 발행. web 의 healthy
판정도 같은 상수를 쓴다.

## Celery beat 스케줄 (`worker/celery_app.py`, timezone=Asia/Seoul)

| 시각(KST) | 태스크 | 내용 |
|---|---|---|
| 매일 18:30 | `ingest_daily_ohlcv` | 일봉 적재 |
| 매일 18:40 | `snapshot_kis_stock_master` | KIS 종목마스터(관리종목·정리매매 플래그) |
| 매일 18:50 | `ingest_daily_snapshots` | 로컬 영구 저장소 선적재 |
| 매시 :10 | `ingest_news` | 언론사 RSS |
| 매일 03:00 / 09:00 | `backup_database` / `check_backup_freshness` | DB 백업·신선도 감시 |
| 매일 04:00 | `cleanup_old_alerts` | alerts 보존정책 |
| 월요일 09:00 | `check_fill_quality_drift` | 슬리피지 실측 드리프트 |
| 분기초(1/4/7/10월 1일) 19:00 | `snapshot_sector_map` | 업종분류 스냅샷(PIT) |

## ⚠️ 반복 실수 지점

- **컨테이너 TZ 는 UTC 인데 KRX 거래일은 KST.** `celery_app.timezone` 은 beat 스케줄 표시에만
  적용되고 **태스크 본문의 `date.today()` 에는 영향이 없다.** 거래일을 다룰 땐
  `app.services.market.now_kst().date()` 를 쓴다(§65 에서 이 함정으로 실제 결함이 났다).
- **web/engine/worker 는 핫리로드가 없다.** 코드 수정 후 `docker compose restart <svc>`.
  (개발 중엔 `docker-compose.override.yml` 이 병합돼 web 만 `--reload` 로 뜬다.)
- **프론트 패키지는 컨테이너 내부에 설치.** 호스트 `npm install` 은 익명 볼륨에 가려 반영 안 됨.
  → `docker compose exec frontend npm install <pkg>`
- **시크릿은 `.env` 가 아니라 `secrets/*.txt` 파일 마운트.** 새 시크릿은 compose secret +
  `app/core/config` 배선.
- **빌드 산출물 커밋 금지** (`frontend/tsconfig.tsbuildinfo` 등).

앱을 실제로 띄워 검증·스크린샷은 `run-quantfolio` 스킬을 쓴다.

## CI (GitHub Actions)

| 워크플로 | 트리거 | job |
|---|---|---|
| `.github/workflows/ci.yml` | main 대상 PR + main push | `backend`(pytest+커버리지) · `frontend`(lint→**tsc --noEmit**→vitest→build) |
| `.github/workflows/e2e-smoke.yml` | 매일 03:00 KST + 수동 | `smoke` — compose 전체 스택 기동 후 Playwright 로 "가입→로그인→전략목록→백테스트→모니터링" |

- **CI 는 `npx tsc --noEmit`(타입체크)를 돈다.** 로컬 게이트(lint→vitest→build)에는 이 단계가
  없으므로, 프론트를 고쳤으면 타입체크도 같이 돌리는 편이 CI 왕복을 줄인다.
- backend job 은 timescaledb·redis 서비스 컨테이너를 띄우고 `alembic upgrade head` 후 pytest 를 돈다
  — **마이그레이션이 깨지면 CI 가 테스트 전에 죽는다.**
- e2e-smoke 는 스택 기동이 무거워 **PR 필수 게이트가 아니다**(야간 크론).
- 상태 확인·실패 로그 요약은 `ci-check` 스킬.

## 이 저장소의 Claude Code 훅 (동작이 눈에 안 보임)

| 훅 | 시점 | 동작 |
|---|---|---|
| `block-secret-edit.js` | PreToolUse(Edit\|Write) | `secrets/*.txt`·`.env*` **편집을 차단**한다. 실거래 자격증명 노출 방지 — 막히면 훅 때문이지 권한 문제가 아니다 |
| `backend-restart-reminder.js` | PostToolUse(Edit\|Write) | `backend/**` 수정 시 web/engine/worker 재시작 리마인더(핫리로드 없음) |
