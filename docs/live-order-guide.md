# 실주문 실행 가이드 — 모의투자 / 실전

이 문서는 리밸런싱 전략(예: `id=23`)의 주문을 **실제로 증권사에 전송**하는 두 경로를 정리한다.

- **모의투자(vts)**: KIS 모의투자 계좌로 실제 주문 전송(가상 체결). 손실 위험 없음.
- **실전(prod)**: KIS 실계좌로 실제 주문 전송. **실제 돈이 나감.**

> ⚠️ 기본 상태는 항상 **모의투자(`KIS_ENV=vts`)** 이며, 미리보기/스크립트는 옵션 없이는 어떤 주문도 내지 않는다. 이는 오발주 방지를 위한 의도된 안전장치다.

---

## 0. 두 실행 경로의 차이 (먼저 이해할 것)

주문을 내는 경로는 **두 가지**이고 목적이 다르다. 혼동하면 오발주로 이어진다.

| | ① 수동 스크립트 | ② 실시탭(자동 엔진) |
|---|---|---|
| 트리거 | 사람이 터미널에서 **1회 수동** | 프론트 실시탭 **start** 버튼 |
| 파일 | `scripts/paper_rebalance.py` | `engine/main.py` (engine 컨테이너 상주) |
| 동작 | 지금 즉시 1번 주문하고 종료 | 상주하며 **스케줄대로 반복** |
| 전략 status | `backtested` 그대로 | `LIVE` 로 전환 |
| 발화 게이트 | 장중·cadence **무시**(강제 1회) | quarterly·`rebalance_time`·regime 준수 |
| 엔진 재기동 복구 | 없음 | 자동 복구(status=LIVE 기준) |
| 용도 | "지금 한 번 담아본다" | "지속 자동 운용" |

- **오늘 당장 체결을 보고 싶다** → ① 수동 스크립트
- **계속 자동으로 굴리고 싶다** → ② 실시탭 start

두 경로 모두 아래 **환경 스위치(`KIS_ENV`)** 를 공유한다 — `vts` 면 모의, `prod` 면 실전.

---

## 1. 모의투자 주문을 실제로 가게 하는 법

### 전제 조건
- `KIS_ENV=vts` (기본값). `.env` 또는 환경변수로 설정.
- KIS 모의투자 앱키/시크릿이 등록되어 있어야 함(DB 등록 또는 `secrets/kis_app_key.txt` 등).
- 사용자 계정에 KIS 자격증명이 연결되어 있을 것.

### 1-A. 수동 스크립트로 1회 실행 (권장 — 가장 안전)

```bash
# ① 미리보기 — 주문 안 냄. '지금 리밸런싱하면 낼 주문'만 출력.
docker compose run --rm web python scripts/paper_rebalance.py --strategy 23

# ② 실제 모의 주문 전송 — --execute 를 붙여야만 주문이 나감.
docker compose run --rm web python scripts/paper_rebalance.py --strategy 23 --execute
```

- `--execute` **없으면** 절대 주문하지 않는다(미리보기 전용).
- `--execute` 는 `settings.is_paper_trading`(= `KIS_ENV=vts`)가 **아니면 거부**한다 → 이 스크립트로는 실전 오발주가 구조적으로 불가능.
- 장중/스케줄을 무시하고 **강제 1회** 리밸런싱한다(모의 점검 목적). Redis 의 '마지막 실행일'은 소비하지 않아 정규 스케줄에 영향 없음.
- 낼 주문이 없으면(`드리프트 밴드 내` 또는 `선정 0종목`) `"낼 주문이 없습니다"` 출력 후 종료.

체결/주문 결과는 앱의 **체결 로그** 및 KIS 모의투자 계좌에서 확인한다.

### 1-B. 실시탭으로 지속 자동 운용 (모의)

`KIS_ENV=vts` 인 상태에서 실시탭 **start** 를 누르면, 엔진이 모의투자 계좌로 스케줄에 맞춰 자동 주문한다. 실주문과 코드 경로는 동일하고 **대상 계좌만 모의**다. → 상세 절차는 아래 2번의 실시탭 부분과 동일(단 `KIS_ENV=vts`).

> 주의: 실시탭 자동 운용은 **발화 조건이 좁다**. 예를 들어 `id=23`은 `cadence=quarterly`, `rebalance_time=14:30`, `rebalance_dom=1` 이라 **분기 시작일 14:30 이후 단 한 번**만 신규 주문이 나간다. regime 이 위험회피면 청산만 한다. "장시간 켜둠 ≠ 자주 체결"임을 기억할 것.

#### 콜드 스타트 즉시 발화 옵션 (`initial_fill_immediate`)

전략을 **처음 실시탭에서 시작**할 때 다음 분기 발화일까지 기다리지 않고 **즉시 초기 매수**하려면 전략 config 에 아래를 추가한다.

```json
{ "initial_fill_immediate": true }
```

- **발동 조건(모두 충족)**: 옵션 `true` + 마지막 실행 기록 없음(첫 실행) + 보유 없음 + **장중** + 레짐 위험회피 아님.
- 이때 `rebalance_dom`·`rebalance_time`(발화일/시각)을 **무시**하고 장중이면 즉시 1회 리밸런싱한다.
- 이번 주기의 정기 리밸런싱을 **대체**(스케줄 소비)하므로 중복 매수는 없다. **이후 주기는 정상 cadence** 를 따른다.
- 레짐 위험회피 국면이면 발동하지 않고 현금을 유지한다(청산 경로만).
- 재기동으로 마지막 실행 기록이 남아 있으면 재발동하지 않는다(첫 실행 아님).
- 장 마감 후 start 하면 다음 개장까지 대기했다가 장중 첫 tick 에 발화한다(수동 `--execute` 와 달리 장외 오발주 없음).

---

## 2. 실전(prod) 주문을 실제로 가게 하는 법

> 🔴 여기서부터는 **실제 자금**이 집행된다. 아래 체크리스트를 모두 통과한 뒤에만 진행한다.

### 2-0. 핵심 스위치: `KIS_ENV=prod`
`app/core/config.py` 의 `is_paper_trading` 이 `KIS_ENV != "prod"` 로 판정한다. 즉 **`KIS_ENV=prod` 로 바꾸는 순간** 모든 주문이 실계좌(`KIS_BASE_URL_PROD`)로 전송된다.

### 2-1. 사전 체크리스트
- [ ] **실전용** KIS 앱키/시크릿으로 교체 (모의키와 다름).
  - `secrets/kis_app_key.txt`, `secrets/kis_app_secret.txt` 를 실전값으로 교체, 또는 사용자 DB 자격증명을 실전값으로 갱신.
- [ ] 실전 계좌번호 `KIS_ACCOUNT_NO`(`CANO-PRDT` 형식) 설정.
- [ ] `.env` 에 `KIS_ENV=prod` 설정.
- [ ] `APP_ENV=prod` 설정 → 이 경우 부팅 시 강제되는 것들:
  - `SECRET_KEY`, `CREDENTIAL_ENC_KEY` 누락 시 **부팅 거부**(dev 처럼 임시키 생성 안 함).
  - `COOKIE_SECURE=true` 아니면 **부팅 거부**(토큰 탈취 방지).
- [ ] 전략 config 의 `capital`·`drift_band_pct`·리스크 한도(`RiskLimit`)를 실계좌 규모에 맞게 재확인.
- [ ] 모의(vts)에서 동일 전략을 충분히 검증 완료.

### 2-2. 실전 전환 절차

```bash
# 1) 실전 시크릿 교체
#    secrets/kis_app_key.txt, secrets/kis_app_secret.txt 를 실전값으로 저장

# 2) .env 편집
#    KIS_ENV=prod
#    APP_ENV=prod
#    COOKIE_SECURE=true
#    KIS_ACCOUNT_NO=<실전 계좌: CANO-PRDT>

# 3) 재기동 (engine/web 이 새 KIS_ENV 로 뜨도록)
docker compose up -d --force-recreate web engine

# 4) 엔진 생존 확인
docker compose logs -f engine   # "매매 엔진 시작 — KIS_ENV=prod (모의투자=False)" 확인
```

> ⚠️ 수동 스크립트 `paper_rebalance.py --execute` 는 `KIS_ENV=prod` 에서 **의도적으로 거부**된다. 실전 주문은 **반드시 실시탭(자동 엔진) 경로**로만 나가도록 설계되어 있다. 실전에서 이 스크립트로 주문을 낼 수 없다.

### 2-3. 실전 자동매매 시작 (실시탭)

1. 웹 로그인 → 대상 전략 화면 → **실시탭**.
2. **start** 버튼 → `POST /api/engine/strategies/{id}/start`.
   - KIS 자격증명 없으면 400.
   - 성공 시 `status=LIVE` 전환 + Redis `engine:control` 로 start 명령 발행.
3. 엔진(`engine/main.py`)이 명령을 받아 러너를 상주 기동.
4. 이후 엔진이 전략 스케줄(cadence·rebalance_time·regime)에 따라 **자동으로** 실계좌 주문을 전송한다.

### 2-4. 확인·중지
```bash
# 엔진 상태
curl http://localhost:8000/api/engine/status    # {"engine_alive": true}

# 활성 전략 집합 (Redis)
#   engine:active_strategies 에 전략 id 가 들어있으면 러너 상주 중.

# 중지: 실시탭 stop 버튼 → status=backtested, 엔진에 stop 명령 발행
```

---

## 3. 주문이 안 나갈 때 체크리스트

수동/자동 어느 경로든 "체결 0건"이면 아래 순서로 확인:

1. **수동인데 `--execute` 를 안 붙였다** → 미리보기라 원래 무주문. (가장 흔함)
2. **자동인데 status 가 `backtested`** → 실시탭 start 가 안 걸림. `engine:active_strategies` 에 id 없음.
3. **발화 조건 미충족** → 오늘이 cadence 발화일/시각이 아님(예: quarterly 14:30 이전, 이번 분기 이미 실행).
4. **선정 0종목** → PIT 유니버스 조회 실패로 `config.universe`(빈 배열) 폴백 → targets 비어 무주문. 미리보기로 `후보풀 크기`·`목표비중` 확인.
5. **regime 위험회피** → 청산만 하고 신규 매수 안 함. 보유 없으면 "매매 없음".
6. **드리프트 밴드 내 / 수량 0주** → 목표비중이 밴드 미만이거나 `목표금액/가격 < 1주`(고가주가 `floor 0`)면 해당 종목 스킵.
7. **KIS 자격증명/계좌 미설정** → 주문 거부(REJECTED). 엔진/스크립트 로그 확인.

미리보기(`--execute` 없이)로 `후보풀 크기`, `레짐 위험회피`, `목표비중`, `산출 주문`을 먼저 찍어보면 위 대부분을 즉시 판별할 수 있다.

---

## 4. 안전장치 요약 (설계 의도)

- **safe default**: `KIS_ENV` 기본 `vts`(모의), 스크립트 기본 미리보기(무주문).
- **명시적 opt-in**: 실주문은 `--execute` 또는 실시탭 start 로만.
- **이중 거부**: 수동 스크립트는 `KIS_ENV=prod` 에서 `--execute` 를 거부 → 실전 주문은 엔진 경로로만.
- **멱등성 3중 방어**: 결정적 idempotency_key + Redis 분산 락(SET NX) + `orders.idempotency_key` UNIQUE → 중복 주문 차단.
- **리스크 게이트**: 일일 손실 한도·`max_position`·손절 평가를 통과해야 주문 실행.
