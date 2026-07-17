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
- [ ] **실전 전환 게이트(`app/services/live_gate.py`) 승인** — 아래 2-1-0 절 참고.
- [ ] **실시간 체결통보(`engine/fill_notice.py`) 전환 체크리스트** — 아래 2-1-A 절 참고.

### 2-1-0. 실전 전환 게이트(`live_gate.py`) — 승인 플래그·운영 절차

부팅 검증(`_ensure_prod_approval`, `app/core/config.py`)과는 별개로, **주문·전략 기동 시점**마다
`evaluate_live_gate(db, user_id, strategy_id, order_notional=...)`가 다음 3조건을 순차 평가해
하나라도 실패하면 주문/기동을 거부한다(`engine/base_runner.py`의 `_place` 직전, `engine/main.py`의
`_start_strategy` 진입점에 배선됨). **vts(모의투자)에서는 체크는 수행하되 로깅만 하고 항상 통과**시켜
실거래 이전에 게이트 동작을 미리 관찰할 수 있다.

1. **체결 정합 실측 등급** — 최근 90일 리밸런스 표본의 M1(실행 슬리피지)/M3(총 정합 괴리)
   등급이 RED면 차단. 표본이 `DEFAULT_MIN_SAMPLE`(기본 30) 미만이면 "판단 불가"로 **보수적으로 차단**
   (표본이 쌓일 때까지는 실전 전환 자체가 불가능하다는 뜻 — §4 캘리브레이션 카드로 표본·등급을 모니터링할 것).
2. **주문 금액 상한** — 1회 주문은 `RiskLimit.max_position_size`(전략별 우선, 없으면 사용자 공통),
   일일 누적은 `KIS_DAILY_ORDER_NOTIONAL_CAP`(`.env`, 원 단위, None/0이면 미적용) 대비 당일 체결 합계.
3. **prod 2단계 승인** — `KIS_PROD_APPROVED`(기본 `False`)가 꺼져 있으면 실전 주문 자체를 거부.
   부팅 시에도 `KIS_ENV=prod`인데 이 플래그가 꺼져 있으면 프로세스가 아예 기동을 거부한다
   (`_ensure_prod_approval`) — 게이트와 부팅 검증 이중 방어.

**운영 절차:**
- [ ] 모의(vts)에서 전략을 충분히 돌려 정합 실측 표본을 `DEFAULT_MIN_SAMPLE` 이상 확보한다
  (§4 monitor 페이지 정합 리포트 카드에서 표본 수·등급 확인).
- [ ] M1/M3 등급이 RED가 아님을 확인한다. RED면 §4 캘리브레이션 제안을 검토·적용하거나
  전략 로직을 재점검한 뒤 재측정한다.
- [ ] `KIS_DAILY_ORDER_NOTIONAL_CAP`을 실계좌 규모에 맞게 `.env`에 설정한다(미설정 시 무제한이므로
  반드시 값을 넣을 것).
- [ ] 위 조건을 모두 확인한 뒤에만 `KIS_PROD_APPROVED=true`를 `.env`에 설정하고 재기동한다
  (§2-2 실전 전환 절차의 일부로 함께 적용).
- [ ] 전환 후에도 게이트는 매 주문·기동마다 계속 평가된다 — 정합 등급이 나중에 RED로 떨어지면
  `KIS_PROD_APPROVED`가 켜져 있어도 신규 주문이 자동으로 차단된다(운영 중 안전망).

### 2-1-A. 실시간 체결통보(fill_notice) 실계정 전환 체크리스트

`engine/fill_notice.py` 는 KIS 체결통보(H0STCNI0/H0STCNI9) WS 를 사용자(계좌)당 1개
구독해 체결을 실시간 반영하는 골격이다. **실계정이 없어 종단(라이브 접속) 검증을
하지 못한 상태로 구현**되었으므로, 실전 전환 전 반드시 아래를 모의투자 계좌로 먼저
검증한다.

- [ ] **`KIS_HTS_ID` 설정** — 체결통보 구독의 tr_key(HTS 로그인 ID). 미설정이면
  `FillNoticeManager` 가 로그만 남기고 아무 것도 구독하지 않는다(안전한 기본값이지만,
  이 상태로는 체결통보 경로 자체가 비활성이라 executor 즉시체결 + reconcile 폴링
  2중 경로에만 의존하게 된다).
- [ ] **모의투자(vts)로 먼저 검증** — `KIS_ENV=vts`, `KIS_HTS_ID` 설정 후 실시탭으로
  전략을 켜고, `docker compose logs -f engine | grep 체결통보` 로 구독·수신·반영
  로그가 정상적으로 찍히는지 확인한다.
- [ ] **CNTG_QTY(체결수량) 필드가 누적값인지 증분값인지 실제 로그로 확인** —
  `engine/fill_notice.py` 는 이 필드를 "해당 주문의 누적 체결수량"으로 가정해
  `reconcile.py._order_recorded_qty` 와 동일한 델타 방식을 적용한다. 부분체결이
  여러 번 나는 주문으로 실제 프레임을 로깅해, 회차별 CNTG_QTY 값이 매번 늘어나는
  누적값인지 매번 이번 회차분만 담긴 증분값인지 반드시 확인할 것. 증분값으로
  밝혀지면 `parse_fill_notice`/`apply_fill_notice` 의 델타 계산 로직을 증분 합산
  방식으로 변경해야 한다(현재 구현은 누적 가정으로 델타=notice.qty−already 계산).
- [ ] **tr_id 실전 전환 확인** — `settings.is_paper_trading` 에 따라 자동으로
  모의(`H0STCNI9`) ↔ 실전(`H0STCNI0`) 이 분기되므로 별도 설정은 필요 없지만,
  `KIS_ENV=prod` 전환 직후 엔진 로그에서 `tr_id=H0STCNI0` 로 구독됐는지 확인한다.
- [ ] **ORGNO 매칭 확인** — REST 주문 응답의 `KRX_FWDG_ORD_ORGNO` 는
  `Order.kis_order_org_no` 로 저장되지만(2026-07 추가), 조사한 바로는 KIS 체결통보
  WS 프레임의 표준 필드셋에 ORGNO 가 노출되지 않아 현재 매칭은 `kis_order_id`
  (ODNO)만으로 이뤄진다. 실계정 프레임을 받아보고 ORGNO 에 해당하는 필드가
  실제로 존재하면 `parse_fill_notice`/매칭 쿼리를 갱신해 이중 확인하도록
  보강할 것.
- [ ] **3중 경로 멱등 확인** — 체결은 ① executor(주문 직후 1회 조회) ② reconcile
  (주기 폴링) ③ fill_notice(실시간 통보) 세 경로가 동시에 존재한다. 세 경로 모두
  `engine/fills.py::record_fill` 을 단일 진입점으로 쓰고, reconcile·fill_notice
  는 "이미 기록된 누적 체결수량(executions 합)" 대비 델타만 반영하므로 정상적으로는
  중복 기록되지 않는다. 실계정 전환 후 `executions` 테이블에서 같은 주문(order_id)에
  대해 합계가 `orders.qty` 를 초과하지 않는지(과다 체결 기록 없음) 반드시 점검한다.
- [ ] **필드 인덱스 재검증** — `parse_fill_notice` 의 필드 순서(23개)는 KIS 공식
  샘플 순서를 따른 것으로, 실계정 프레임으로 종단 검증되지 않았다. 실계정에서 받은
  원본 평문(복호화 직후, 로그에는 남기지 말고 임시로만 확인)과 필드 인덱스가
  일치하는지 최초 1회 반드시 대조한다.

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

## 3-1. MDD 킬스위치 발동 → 점검 → 재개 체크리스트

`risk_layer.mdd_kill_pct` 가 설정된 전략은 고점(HWM) 대비 낙폭이 임계를 넘으면 **전량
청산·현금 대피**한다(`engine/rebalance_runner.py::_evaluate_mdd_kill`). 발동 즉시 critical
알림이 앱 내(WS)·텔레그램(B-1 설정 시)으로 발행된다(`code=mdd_kill`).

> ⚠️ **현재 동작(2026-07 기준)**: 발동 후 `risk_layer.mdd_rearm_days`(기본 20) **거래일이
> 지나면 엔진이 자동으로 재가동**하고, 그 시점의 자산가치를 새 고점 기준선으로 삼아 다음
> 정기 리밸런싱부터 다시 매수한다. 즉 **사람이 아무것도 하지 않아도 재진입한다.**

### 발동 시 확인 사항
1. 알림(텔레그램/앱)에 찍힌 낙폭·고점·현재 자산가치를 확인한다.
2. `docker compose logs engine | grep "MDD 킬스위치"` 로 발동 시각과 사유를 재확인한다.
3. 낙폭이 **전략 로직의 결함**(팩터 계산 오류·유니버스 오염 등)인지 **시장 전반의
   급락**(정상적인 리스크 관리 작동)인지 구분한다 — 전자면 재가동 전에 반드시 원인을
   해소해야 한다.
4. 다른 전략들도 같은 시점에 청산됐는지 확인해 개별 전략 문제와 시장 전체 문제를 구분한다.

### 재개 시 권장 절차 (자동 재가동을 신뢰하지 않는 경우)
자동 재가동 전에 사람이 개입하고 싶다면, 재가동 창(쿨다운 만료) 전에 **실시탭에서 해당
전략을 stop** 한다. 중지된 전략은 재가동 로직 자체가 실행되지 않으므로(엔진이 러너를
아예 돌리지 않음) 사실상 "수동 승인 대기" 상태가 된다. 점검이 끝나면 실시탭에서 다시
**start** 해 재개한다 — 이 경우 콜드 스타트로 취급되어 `initial_fill_immediate` 설정에
따라 즉시 매수하거나 다음 정기 리밸런싱을 기다린다(§1-B 참고).

### 개선 방향(TODO, 미구현)
구 개선안(improvement-plan-2026-07-16, git 히스토리) B-3 은 "재개는 반드시 수동 조작으로 제한"을
권장한다 — 즉 쿨다운 경과만으로 자동 재가동하지 말고, 사람이 명시적으로 승인해야만
`killed` 상태를 해제하도록 `_evaluate_mdd_kill` 을 바꾸는 것. 이는 실거래 안전성을 바꾸는
변경이라 별도 논의·구현 작업으로 남겨둔다(현재는 위 "실시탭 stop→점검→start" 수동
절차로 동일한 효과를 낼 수 있으니, 쿨다운을 기다리지 않고 점검하고 싶다면 이 절차를
쓸 것).

---

## 3-2. 다중 전략 병행 운용 전 — 마이그레이션 0008 백필 감사

`0008_multi_strategy_positions`(Position/Execution에 `strategy_id` 추가)는 기존 데이터를
`orders` 이력으로 역산해 백필했다. 같은 (user, symbol)에 **복수 전략의 주문 이력이 섞여
있어 소유 전략을 유일하게 특정할 수 없는 경우 보수적으로 `strategy_id=NULL`**(비귀속)로
남겼다. id=23+24처럼 종목이 겹칠 수 있는 전략을 **병행 운용하기 전** 이 목록을 반드시 감사한다.

### 감사 절차

```bash
# 1) NULL로 남은 포지션 확인 (미청산만 — qty=0인 과거 청산분은 무관)
docker compose exec db psql -U quantfolio -d quantfolio -c "
  SELECT user_id, symbol, qty, avg_price
  FROM positions
  WHERE strategy_id IS NULL AND qty > 0
  ORDER BY user_id, symbol;"

# 2) 해당 (user, symbol)의 주문 이력에서 실제로 몇 개 전략이 관여했는지 확인
docker compose exec db psql -U quantfolio -d quantfolio -c "
  SELECT user_id, symbol, strategy_id, side, count(*), min(created_at), max(created_at)
  FROM orders
  WHERE (user_id, symbol) IN (
    SELECT user_id, symbol FROM positions WHERE strategy_id IS NULL AND qty > 0
  )
  GROUP BY user_id, symbol, strategy_id, side
  ORDER BY user_id, symbol, strategy_id;"
```

### 판단 가이드
- **주문 이력의 모든 행이 사실상 전략 하나에서만 나왔다면** — 사람이 확인 후 수동으로
  `UPDATE positions SET strategy_id = <id> WHERE ...`로 귀속시킬 수 있다. 이 경우
  `uq_positions_user_strategy_symbol` 제약과 충돌하지 않는지(같은 전략의 다른 행 존재 여부)
  먼저 확인할 것.
- **정말로 여러 전략이 같은 종목을 거래한 이력이 섞여 있다면** — 귀속 전략을 확정할 수
  없으므로 NULL로 유지하고, 실제로는 계좌 뷰(`GET /api/trading/positions` 등)에서만 보이는
  "수동/레거시" 포지션으로 취급한다. 이 포지션은 §1 전략 삭제 가드나 §3 전략별 리스크
  집계에서 자동으로 제외된다(전략 스코프 계산에 포함되지 않음, 계좌 스코프에는 계속 포함).
- id=23+24 등 종목이 겹칠 수 있는 조합을 병행 운용하기 시작한 **이후**에는 신규 주문이
  항상 `strategy_id`를 명시적으로 채우므로(§2 이전 스프린트에서 확인, `executor.py`의
  `execute_signal`이 `strategy_id: int`를 필수로 받음) 이 문제가 재발하지 않는다 — 감사는
  **과거 이력에 한해 1회성**으로 충분하다.

---

## 4. 안전장치 요약 (설계 의도)

- **safe default**: `KIS_ENV` 기본 `vts`(모의), 스크립트 기본 미리보기(무주문).
- **명시적 opt-in**: 실주문은 `--execute` 또는 실시탭 start 로만.
- **이중 거부**: 수동 스크립트는 `KIS_ENV=prod` 에서 `--execute` 를 거부 → 실전 주문은 엔진 경로로만.
- **멱등성 3중 방어**: 결정적 idempotency_key + Redis 분산 락(SET NX) + `orders.idempotency_key` UNIQUE → 중복 주문 차단.
- **리스크 게이트**: 일일 손실 한도·`max_position`·손절 평가를 통과해야 주문 실행.
