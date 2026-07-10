# 코드 리뷰 결과 — 2026-07-10 (`/code-review max`, 프로젝트 전체)

리뷰 대상: 백엔드(엔진·백테스트·메트릭·데이터 계층) + 데이터 라이브러리 통합.
방법: max effort 다중 에이전트 finder → 직접 코드 검증(1-vote). 세션 한도로 일부 에이전트가
조기 종료되어 아래 **커버리지** 절에 명시한 3개 영역은 별도 재실행으로 보완.

심각도: 🔴 High / 🟠 Medium / 🟡 Low

---

## 🔴 High

### 1. drift_band가 신규 편입 종목의 최초 매수를 막음 (라이브 + 백테스트)
- **위치**: `backend/engine/rebalance.py:284`, `backend/app/services/backtest/portfolio.py:379`
- **내용**: `if abs(cur_w - target_w) <= drift_band: continue`. 신규 종목은 `cur_w=0`이므로
  `목표비중 <= drift_band`이면 영원히 매수되지 않는다.
- **재현**: 기본 `drift_band_pct=0.05` + 등비중 `top_n=20` → 종목당 비중 정확히 0.05 →
  `abs(dev)=0.05 <= 0.05` → 전 종목 스킵 → 포트폴리오가 100% 현금으로 고착.
  `top_n=21`이면 0.0476 < 0.05로 아예 한 주도 매수 못 함. score/inverse_vol 가중에서도
  하위 소형비중 꼬리가 통째로 누락된다. (`rebalance.py:22-24` 주석에 저자도 inverse_vol
  한정으로 인지하고 있으나, 등비중 기본 조합에서 라이브 주문까지 영향.)
- **수정**: 신규 편입(`cur_w==0`)은 밴드 예외 처리하거나 부등호를 `<`로.
- **✅ 조치(2026-07-10)**: 라이브·백테스트 양쪽에서 드리프트 밴드를 *보유 종목 비중
  미세조정*에만 적용하도록 게이트를 좁힘. `is_new_entry`(미보유→목표>0)·`is_full_exit`
  (목표0→보유>0)는 밴드 무관 항상 체결. `rebalance.py:284`, `portfolio.py:379` 수정.

### 2. PIT 지수 구성종목 조회 실패 시 빈 결과를 영구 캐시 (캐시 오염)
- **위치**: `backend/app/services/data/krx_index.py:114`
- **내용**: `_MEMBERS_CACHE[key] = codes`를 성공 여부와 무관하게 무조건 캐시.
- **재현**: 7일 재시도가 모두 일시적 네트워크/인증 오류로 실패하면 `[]`가 프로세스 수명 내내
  그 `(지수,날짜)` 키에 고정되고, 러너는 `config.universe` 고정 폴백으로 조용히 전환된 채
  재시작 전까지 복구 불가. 바로 아래 `all_listed_stocks`는 "성공만 캐시"(125행 주석)로
  올바르게 처리 — 동일 파일 내 불일치.
- **수정**: `if codes:`일 때만 캐시(실패는 캐시 금지).
- **✅ 조치(2026-07-10)**: `if codes:` 가드 추가 — 빈 응답은 캐시하지 않아 다음 호출에
  자동 재시도. `all_listed_stocks`/`market_caps`의 성공-only 캐시 패턴과 일치. `krx_index.py:114`.

---

## 🟠 Medium

### 3. `socket.setdefaulttimeout` 프로세스 전역값을 동시 러너가 서로 덮어씀
- **위치**: `backend/app/services/data/loader.py`(`bounded_socket_timeout`),
  `backend/app/services/market.py:37-49`(`is_business_day`), `backend/engine/main.py:44`
- **내용**: 전역 소켓 타임아웃을 save/restore하는데 이는 재진입 불가·비스레드안전.
- **재현**: 여러 전략 러너가 동시에 `compute_universe_scores`(to_thread)와
  `is_business_day`(이벤트루프 동기 호출)를 실행하면 `prev` 캡처가 서로의 변경값을 잡아
  restore가 엉킨다. 예: 기준 30 → A가 20 설정 → B가 prev=20 캡처 → A가 30 복원 →
  B가 20 복원 → 전역이 20에 영구 잔류. 최악은 `is_business_day`(5초)와 겹쳐 baseline이
  5초로 낮아져 정상적이지만 느린 응답에 스퓨리어스 타임아웃, 또는 None 잔류로 무한대기 —
  **PR이 막으려던 무한대기를 동시성으로 되살릴 수 있음**.
- **수정**: 요청별 `timeout=` 인자 사용, 또는 전역 변경을 락으로 직렬화(권장: 전역 mutate 대신
  각 라이브러리 호출에 명시적 timeout).
- **✅ 조치(2026-07-11)**: `bounded_socket_timeout` 을 `threading.RLock`+활성요청 리스트로 재작성.
  baseline 은 활성 요청이 0→1 될 때 한 번만 캡처, 1→0 될 때 한 번만 복원(중첩·동시성에서 None
  잔류 불가). 활성 중엔 요청들의 `min` 타임아웃 유지. `market.py:is_business_day` 의 자체 인라인
  전역조작을 제거하고 동일 CM 을 쓰도록 통일(두 번째 경합 지점 해소). `loader.py`, `market.py`.

### 4. 일시적 pykrx 지연 시 모멘텀 팩터가 조용히 전량 소멸
- **위치**: `backend/app/services/metrics.py:532-551`
- **내용**: `_fetch_price_change`가 타임아웃 예외를 흡수해 빈 DF 반환 →
  `pc_21d/63d/126d.empty` → 모든 종목의 `mom_1m/3m/6m` 미설정 → 모멘텀(기본 가중 0.4)이
  전 종목 NaN.
- **재현**: 스코어러가 남은 밸류/저변동만으로 재정규화해 에러 없이 완전히 다른 목표
  포트폴리오를 산출, 러너가 그대로 주문. 단발성 KRX 지연이 한 번의 리밸런싱을 왜곡된 팩터로
  실행시킨다.
- **수정**: 핵심 팩터 조회 실패 시 리밸런싱 스킵(다음 주기 재시도) 또는 최소 유효 팩터 수 가드.
- **✅ 조치(2026-07-11)**: 모멘텀 가중치>0 인데 등락률 21/63/126일 조회가 **모두** 빈 응답이면
  (개별 종목 NaN 이 아닌 KRX 전면 장애) `RuntimeError` 를 올리도록 가드 추가. 러너 틱 루프
  (`rebalance_runner.py:105-112`)가 예외를 잡아 이번 주기를 건너뛰고 다음 주기에 온전한 팩터로
  재시도한다(왜곡 포트폴리오 주문 방지). `metrics.py:compute_universe_scores`.

### 5. 사이즈 중립화 β 분모 표본 불일치 → 중립화가 불완전
- **위치**: `backend/app/services/metrics.py:355,368`
- **내용**: 무절편 OLS 사영 β는 `Σ_m(s·x)/Σ_m(x²)`여야 하는데, 분자는 `m = valid & s.notna()`
  행만 합산(368), 분모 `denom`은 `valid`(팩터 NaN 포함) 전체의 `Σx²`(355).
- **재현**: `m ⊆ valid`라 분모가 과대 → β가 0쪽으로 편향 → 사이즈 노출 일부만 제거.
  모멘텀이 신규 소형주에서 NaN인 경우 그 로그시총 제곱이 분모만 부풀려 잔차에 소형주 틸트가
  남는다(관측된 id=23 소형주 틸트와 정합).
- **수정**: 분모도 동일 `m` 행으로: `denom_m = float((x[m]**2).sum())`.
- **✅ 조치(2026-07-11)**: 팩터별 β 분모를 분자와 동일 표본 `m` 의 `Σ(x²)` 로 계산하도록 수정
  (`denom_m = float((xm**2).sum())`). 전역 `denom` 제거. 사이즈 노출이 완전 제거되어 잔차 소형주
  틸트가 사라진다. `metrics.py:_neutralize_size`.

### 6. `rebalance_dom`이 해당 월 일수를 넘으면 그 주기를 통째로 건너뜀
- **위치**: `backend/engine/rebalance.py:348`
- **내용**: `if now.day < int(cfg.get("rebalance_dom") or 1): return False`. `rebalance_dom`에
  상한 검증이 없음(schema에 `le=`/clamp 없음 확인).
- **재현**: dom=31·monthly → 4/6/9/11월은 `now.day` 최대 30 < 31이라 그 달 발화 안 함
  (period_key가 월단위라 스킵 확정). dom=29·30이면 2월도 스킵. quarterly도 동일.
- **수정**: 생성 시 dom을 1–28로 클램프하거나 "월 말일 초과 시 해당 월 마지막 영업일" 처리.
- **✅ 조치(2026-07-11)**: 발화 판정에서 dom 을 `min(max(dom,1), calendar.monthrange(y,m)[1])` 로
  해당 월 일수에 클램프. dom=31 이어도 4/6/9/11월은 30일, 2월은 28·29일에 발화(주기 통째 스킵
  방지). `rebalance.py:is_rebalance_due`.

### 7. `ffill()`이 상장폐지 종목 가격을 동결해 백테스트 성과를 상방 편향
- **위치**: `backend/app/services/backtest/portfolio.py:724` (+ 마크투마켓 루프)
- **내용**: `panel = _normalize_index(close_panel).ffill()`로 폐지 종목의 마지막 가격이 이후
  전 구간 유지, 마크투마켓은 양수 가격일 때만 갱신.
- **재현**: 보유 종목이 갭다운 후 폐지돼도 평가액이 폐지 직전가로 동결되고 다음 리밸런싱에서
  그 가격으로 "매도"되어 손실이 P&L에 반영 안 됨. 폭락·폐지한 이름일수록 total_return·Sharpe가
  낙관 편향.
- **수정**: 폐지 이후 구간은 ffill하지 말고 폐지손실 반영(폐지일 이후 NaN 유지 후 청산가 적용).
- **✅ 조치(2026-07-11, financial-expert 검증 반영)**: `ffill(limit=delisting_gap_days)`(기본 10거래일)로
  단기 휴장·거래정지만 메우고, 마크투마켓 루프에서 가격이 결측이 된 보유 종목을 회수율
  (`delisting_recovery`)만 현금화·포지션 소멸로 폐지 손실 확정. `portfolio.py:simulate`.
  - **terminal-gap 게이팅(핵심)**: write-off 는 `last_valid_index` 기준 **마지막 유효 관측 이후
    두 번 다시 값이 없는 경우(진짜 폐지)에만** 발동. 상장적격성 실질심사·개선기간처럼 수주~1년
    거래정지 후 '재개'되는 interior gap 을 폐지로 오분류해 영구 write-off 하던 결함을 제거(재개형은
    평가액 동결 유지). — financial-expert 1순위 권고.
  - **회수율 기본값 0.9(2026-07-11 실측 확정)**: pykrx 가격 패널의 종점가는 이미 최종 회수가치를
    담는 것으로 실측 확인. ① 자진/공개매수 상폐(쌍용C&E 003410) → 종점가 = 공개매수가(7,000원),
    ② 부실/회생 상폐(한진해운 117930) → 종점가 = 정리매매 폭락 확정가(780→12원, −98%). 즉 write-off
    직전 `val[sym]` 은 이미 공정한 최종가로 mark-to-market 되어 있어, 낮은 회수율은 두 유형 모두
    **이미 올바른 값을 추가로 깎는 계통적 이중 페널티**가 됨. 기본값을 0.35→**0.9** 로 상향(관측일~
    write-off일 1일 갭·정리매매 유동성 비용 반영 소폭 haircut). 정리매매 종가가 없는 소스를 쓰는
    유니버스면 config `delisting_recovery` 로 0.35~0.5 하향.
  - **KOSPI200 영향**: 지수 편출은 여전히 정상 거래(NaN 아님)라 이 로직과 무관 → id=23 등 기존
    대형주 백테스트 수치는 사실상 무변화 예상. `markers` 의 `type=="delist"` 개수로 검증 가능
    (KOSPI200 은 0~극소수여야 정상; 다수면 데이터 커버리지 종료 오폐지 신호).

---

## 🟡 Low

### 8. 변동성 스케일 슬리피지가 전체구간 median 사용 (미래참조·내부 불일치)
- **위치**: `backend/app/services/backtest/engine.py:93`
- **내용**: `med = float(vol.median())`를 전체 vol 시계열로 계산해 각 봉 슬리피지 스케일에 사용 →
  미래 변동성이 초기 구간 비용에 반영되는 look-ahead. `portfolio.py`의 `_vol_slippage_map`은
  시점별 `panel.loc[:d]` 중앙값을 쓰는데 여기만 불일치.
- **수정**: 트레일링/시점별 median으로 교체.
- **✅ 조치(2026-07-11)**: `vol.expanding(min_periods=10).median()`(시점별 확장 중앙값)으로 교체.
  각 봉 슬리피지가 그 시점까지의 변동성만 참조 → look-ahead 제거. `portfolio._vol_slippage_map`
  규약과 일치. `engine.py:93`.

### 9. `rebalance_weekday`가 5/6이면 주간 전략이 절대 발화 안 함
- **위치**: `backend/engine/rebalance.py:345`
- **내용**: `if now.weekday() < rebalance_weekday: return False`. 장은 평일(weekday 0–4)에만
  열리는데 weekday=5(토)/6(일) 설정 시 `is_market_open`이 참인 시각엔 항상 `now.weekday()<5`
  → 영구 미발화. 검증 없음.
- **수정**: weekday를 0–4로 클램프/검증.
- **✅ 조치(2026-07-11)**: 발화 판정에서 `min(max(weekday,0),4)` 로 클램프. `rebalance.py:345`.

### 10. Bollinger 밴드 std 규약 불일치 (ddof=1 vs 관례 ddof=0)
- **위치**: `backend/app/services/backtest/signals.py:184`
- **내용**: `close.rolling(period).std()`가 pandas 기본 ddof=1(표본표준편차). 관례적 볼린저는
  모표준편차(ddof=0)로, period=20에서 밴드가 ~2.6% 넓어져 크로스 타이밍이 어긋남. 같은 파일
  `_donchian_squeeze`(267)는 의도적으로 `std(ddof=0)` — 내부 불일치.
- **수정**: `std(ddof=0)`로 통일.
- **✅ 조치(2026-07-11)**: `_bollinger_signals` 의 `std()` → `std(ddof=0)` (모표준편차, 관례 준수).
  `_donchian_squeeze` 와 일치. z-score 신호(232행)는 별개라 미변경. `signals.py:184`.

---

## 오탐 (검증 후 제외)
- **DB 멱등성 유니크 제약 부재 의심**: `orders.idempotency_key`(`uq_orders_idempotency_key`),
  `positions(user_id,symbol)`(`uq_positions_user_symbol`) 유니크 제약 존재 확인 → 제외.
- **`is_paper_trading`/`kis_base_url` 불일치 의심**: 둘 다 `KIS_ENV` 파생으로 정합 → 제외.

---

# 보완 리뷰 — API 인증 / 프론트엔드 / DB (재실행 결과)

세션 한도로 조기 종료됐던 3개 영역을 `/code-review max` 재실행으로 보완함.

## API 라우트 인증·권한(IDOR) — ✅ 이상 없음
전 라우트(`deps.py`, `security.py`, `session.py`, `strategies.py`, `backtests.py`,
`trading.py`, `engine.py`, `kis.py`, `auth.py`, `ws.py`, `metrics.py`, `recommend.py`,
`screener.py`, `symbols.py`) 검토 결과 IDOR·미인증 노출·JWT/세션 결함 없음.
- 모든 리소스 핸들러가 `current_user.id`로 소유권 필터(`_get_owned` 헬퍼를 strategies/
  backtests/engine에서 재사용, trading은 `Order.user_id==current.id`/`Position.user_id==
  current.id`, backtest는 `Strategy.user_id==current.id` 조인).
- WebSocket은 클라이언트 입력이 아닌 세션 파생 `engine_events_channel(user_id)`만 구독.
- 세션은 `secrets.token_urlsafe(32)` opaque ID를 Redis에 서버측 저장(JWT 아님), 슬라이딩 TTL,
  로그아웃 시 `destroy_session`. 비밀번호 bcrypt(`checkpw`). KIS 시크릿 Fernet 암호화.
- Pydantic 스키마에 `user_id`/`status`/`role` 등 보호 필드 노출 없음(mass assignment 불가).

## 프론트엔드

### 🟠 F1. 손익 색상 규약이 미국식으로 반전 (한국 시장 관례 위배)
- **위치**: `frontend/lib/format.ts:33` (`trendColor`) + `frontend/app/globals.css:42-43`
- **내용**: `--profit: 152 64% 46%`(녹색), `--loss: 0 72% 56%`(적색) → 상승=녹색·하락=적색.
  KIS HTS·토스증권·삼성증권 등 한국 브로커는 관례적으로 상승=적색·하락=파랑.
- **재현**: 트레이더가 워치리스트에서 +3.2% 종목을 녹색으로 보고, 훈련된 직관상 녹색을 손실로
  순간 오독 → 실시간 모니터링/개입 판단 시 위험. `monitor`·`metrics`·`strategies/[id]` 전반 사용.
- **비고**: 의도적 국제표준 선택일 수 있으므로 제품 결정 확인 필요.
- **✅ 조치(2026-07-11)**: 사용자 결정으로 한국 관례 채택. `globals.css` 의 `--profit`→적(`0 72% 56%`),
  `--loss`→청(`212 78% 56%`)으로 교체(`trendColor`/`text-profit`/`text-loss` 전역 반영). 확인 결과
  매매·팩터 테이블(`strategies/[id]`)은 이미 수익=적/손실=청으로 하드코딩돼 있었고 국제표준은
  토큰뿐이었으므로, 토큰 교체로 앱 전체가 한국 관례로 일관됨. (IR·F-Score 등 '품질' 색상은 P&L
  부호가 아니라 미변경.) `globals.css:42-43`.

### 🟡 F2. `staleTime: Infinity`로 종목명 부분맵이 세션 내내 고착
- **위치**: `frontend/app/strategies/[id]/page.tsx:78`
- **내용**: 동일 `["symbol-names"]` 키를 `monitor/page.tsx`는 자가치유되도록 유한 staleTime +
  `refetchOnWindowFocus`(주석에 Infinity 금지 명시)로 쓰는데, 이 페이지만 `staleTime: Infinity`.
- **재현**: monitor를 거치지 않고 전략 상세를 먼저 연 순간 이름 소스(KRX MDC)가 일시 장애면
  부분/빈 맵 수신 → 이후 백엔드가 자가치유돼도 이 페이지 옵저버는 재조회 안 함 → 세션 내내
  체결 로그가 종목명 대신 코드('005930')만 표시.
- **수정**: monitor와 동일한 유한 staleTime으로 통일.
- **✅ 조치(2026-07-11)**: `staleTime: Infinity` → `60*60*1000` + `refetchOnWindowFocus: true` (monitor
  와 동일). 서버 자가복구 후 재조회되어 종목명 고착 해소. `strategies/[id]/page.tsx:78`.

### 🟡 F3. 로딩 중 엔진 상태가 "중지"로 오표시
- **위치**: `frontend/app/monitor/page.tsx:137`
- **내용**: `engine.data?.engine_alive`를 `engine.isLoading` 확인 없이 사용. 첫 응답 전엔
  `undefined`(falsy) → 적색 "매매 엔진 중지" 확정 상태로 렌더.
- **재현**: 페이지 로드/ WS 재연결 갭마다 ~수백ms 동안, 실제 가동 중이어도 적색 "엔진 중지"가
  깜빡여 사용자가 불필요하게 개입(수동 중지/재시작)할 수 있음.
- **수정**: 로딩 상태를 중립(로딩) 표시로 분기.
- **✅ 조치(2026-07-11)**: `engine.isLoading` 분기 추가 — 첫 응답 전엔 중립(muted) "확인 중…"으로
  표시, 응답 후에만 profit/loss 색으로 가동 여부 판정. `monitor/page.tsx:137`.

## DB 모델·마이그레이션
마이그레이션 체인은 정상(선형 0001→0005, 스키마 드리프트 없음, NOT NULL 추가는 모두
`server_default` 동반, 금액/수량은 모두 `Numeric(18,4)`, 하이퍼테이블 PK에 time 파티션 컬럼 포함).

### 🟠 D1. `executions.order_id` ON DELETE RESTRICT가 유저 삭제 캐스케이드를 차단
- **위치**: `backend/app/models/models.py:215`
- **내용**: `users→orders`는 CASCADE인데 `orders→executions`는 RESTRICT이고, `Order.executions`
  관계에 delete-cascade가 없음.
- **재현**: 계정 삭제/GDPR 파기로 `db.delete(user)` 시 orders CASCADE 삭제가 시도되지만, 체결
  이력(Execution)이 있는 order에서 RESTRICT가 걸려 `foreign_key_violation`으로 트랜잭션 전체
  롤백 → 한 번이라도 체결된 유저는 영구 삭제 불가.
- **수정**: 관계에 `cascade="all, delete-orphan"`(+ passive_deletes) 또는 executions FK를
  `ondelete="CASCADE"`로.
- **✅ 조치(2026-07-11)**: `executions.order_id` FK 를 `ondelete="CASCADE"` 로, `Order.executions`
  관계에 `cascade="all, delete-orphan"`+`passive_deletes=True` 추가. 마이그레이션 `0006` 에서 기존
  FK 드롭·CASCADE 로 재생성. DB 반영 확인(`confdeltype='c'`). `models.py:215`, `alembic/0006`.

### 🟡 D2. FK 인덱스 누락(`orders.strategy_id`, `risk_limits.strategy_id`)
- **위치**: `backend/app/models/models.py:189` 등
- **내용**: `orders.strategy_id`(SET NULL)·`risk_limits.strategy_id`(CASCADE)에 인덱스 없음.
- **재현**: 전략 삭제 시 참조무결성 강제를 위해 orders/risk_limits 전체 스캔, 전략별 주문 조회도
  seq scan → 이력이 쌓인 orders 핫테이블에서 락 경합·지연. (efficiency)
- **수정**: 두 FK 컬럼에 인덱스 추가.
- **✅ 조치(2026-07-11)**: `orders.strategy_id`·`risk_limits.strategy_id` 에 `index=True` + 마이그레이션
  `0006`(`ix_orders_strategy_id`, `ix_risk_limits_strategy_id`). DB 반영 확인. `models.py`, `alembic/0006`.

### 🟡 D3. Enum-as-String 컬럼의 `.value` 접근이 잠재 크래시
- **위치**: `backend/app/models/models.py:193,198` / `backend/engine/executor.py:131`
- **내용**: `side: Mapped[OrderSide] = mapped_column(String(8))`,
  `status: Mapped[OrderStatus] = mapped_column(String(16))` — 컬럼이 순수 `String`이라 DB에서
  로드하면 enum이 아닌 `str`로 복원됨. `order.status.value`(131)는 세션 상주(파이썬이 enum을
  할당한) 객체에서만 동작.
- **재현**: DB에서 새로 로드한(또는 expire 후) Order에 `.status.value`/`.side.value` 호출 시
  `AttributeError: 'str' object has no attribute 'value'`. 현재는 `expire_on_commit=False` +
  세션 내 enum 할당 덕에 우연히 통과 — expire 활성화·refresh 후 재사용 시 파손되는 잠재 결함.
- **수정**: 컬럼을 SQLAlchemy `Enum`/TypeDecorator로 바꾸거나, 접근부에서 `OrderStatus(order.status)`로
  정규화.
- **✅ 조치(2026-07-11)**: `OrderStatus`/`OrderSide` 는 `StrEnum` 이라 `str()` 이 enum·str 양쪽에서
  값 문자열을 반환 → `order.status.value` 를 `str(order.status)` 로 교체(재로드된 str 에도 안전).
  컬럼 타입 변경은 마이그레이션 리스크가 커 접근부 정규화로 처리. `executor.py:131`.

### 🟡 D4. `alembic/env.py`에 `compare_server_default=True` 누락
- **위치**: `backend/alembic/env.py:36`
- **내용**: `compare_type=True`만 설정, `compare_server_default`는 미설정.
- **재현**: `alembic revision --autogenerate`가 server_default 변경을 감지 못 해 누락 → 운영에서
  기본값 없는 NOT NULL 컬럼이 적재된 테이블에 적용돼 마이그레이션 실패 위험.
- **수정**: `context.configure(..., compare_server_default=True)`.
- **✅ 조치(2026-07-11)**: online·offline 양쪽 `context.configure` 에 `compare_server_default=True` 추가.
  `alembic/env.py`.

---

## 종합
- **총 확정**: 백엔드 코어 10건 + 프론트 3건 + DB 4건 = **17건** (API 인증 영역 0건, 이상 없음).
- **가장 시급**: #1(등비중 전략 미체결) · #2(PIT 캐시 오염) — 라이브 매매에서 조용히 오동작.
- **오탐 제외**: DB 멱등성/포지션 유니크 제약(존재), `is_paper_trading`/`kis_base_url`(정합).

## 조치 현황(2026-07-11)
- **수정 완료(17건 전부)**: High #1·#2, Medium #3~#7, Low #8~#10, 프론트 F1·F2·F3, DB D1~D4.
  전 백엔드 테스트 223건 통과, 프론트 `tsc --noEmit` 통과, 마이그레이션 `0006` DB 적용·검증 완료.
- **후속 완료(2026-07-11)**:
  - #7 정리매매/종점가 패널 포함 여부 실측 완료(쌍용C&E·한진해운) → 종점가가 최종 회수가치를
    담음을 확인, `delisting_recovery` 기본값 0.35→**0.9** 확정(#7 항목 상세 참조).
  - 코드·스키마 반영 위해 `web`·`engine`·`worker` 컨테이너 재시작 완료(마이그레이션 head=`0006`).
