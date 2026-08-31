# 전략·팩터 검증 워크플로 (`backend/scripts/`)

이 프로젝트의 **핵심 반복 작업**. 26개 스크립트가 계열별로 나뉜다.
채택·기각 결론은 [`docs/improvements.md`](../../docs/improvements.md)·[`docs/strategies.md`](../../docs/strategies.md)에 누적돼 있으니,
**새 아이디어를 검증하기 전에 이미 기각된 것인지 먼저 확인한다.**

## 실행

```bash
docker compose run --rm web python scripts/<name>.py [옵션]
```

## 계열

| 계열 | 하는 일 | 예 |
|---|---|---|
| `validate_*` | **신규 팩터·전략 후보의 PIT 검증.** id=23 기준선 대비 성과 비교 후 채택/기각 판정 | `validate_flow_factor`·`validate_residual_momentum`·`validate_pead_factor`·`validate_candidates`·`validate_calmgate` |
| `validate_*_ab` | **기능 on/off A/B.** 이미 배선된 옵션이 실제로 성과를 바꾸는지 | `validate_ttm_ab`·`validate_price_limit_ab`·`validate_sector_neutralize_ab` |
| `verify_*` | **구현 정확성 검증.** 합성 데이터로 "의도한 대로 계산되는가" + 실전략 A/B | `verify_fill_model`·`verify_fill_precision`·`verify_risk_layer`·`verify_size_neutral`·`verify_factor_ic`·`verify_benchmark_metrics` |
| `register_and_validate_*` | 후보 전략을 DB에 등록하고 곧바로 검증 | `register_and_validate_abc`·`register_and_validate_volharvest` |
| 튜닝·탐색 | 파라미터 공간 탐색 | `wf_id23`(워크포워드)·`regime_tune`(레짐 파라미터)·`run_panic_overlay_comparison`·`run_panic_placebo` |
| 운영 | 실제 계좌·데이터를 다룸 | `paper_rebalance`(모의 리밸런싱)·`reconcile_fills`(수동 체결정합)·`precompute_panic_series`·`persist_fill_fields` |

## 검증 프로토콜 (`validate_*` 공통 패턴)

```
유니버스: PIT KOSPI200 (universe_rule.source="KOSPI200")   ← 생존편향 제거, 타협 불가
구간:     반기 2-fold(H1/H2) + FULL                        ← 한 구간 성과는 근거가 아니다
기준선:   id=23 (균형 멀티팩터, 레짐 튜닝 완료)
판정:     alpha / Sharpe                                    ← 저베타 전략은 excess/IR 로 보면 안 된다
```

- **손질된 종목 풀에서는 결론이 뒤집힌다.** 동적 섹터로테이션이 +131% → PIT 에서 +12.9% 로 붕괴한 전례가 있다.
- **파라미터를 여러 개 흔들어 최적점을 고르는 것은 과최적화다.** 표본 외·워크포워드 결과를 함께 제시한다.
- 다중검정 보정이 필요하면 `app/services/backtest/deflated_sharpe.py`(DSR).

## 기각해도 배선은 남긴다

전략 등록을 기각해도 **팩터·게이트 구현은 opt-in 능력으로 보존**하는 것이 이 저장소의 관례다
(§41 flow / §42 잔차 모멘텀 / §43 PEAD / 변동성 수확 게이트). 검증 스크립트도 함께 남겨
나중에 재현할 수 있게 한다. 기각 사유는 반드시 `improvements.md` 에 적는다.

## 운영 스크립트 주의

| 스크립트 | 주의 |
|---|---|
| `paper_rebalance.py` | 기본은 **미리보기**(주문 안 나감). `--execute` 를 붙여야 실제 주문이 전송된다. 상세는 [`docs/live-order-guide.md`](../../docs/live-order-guide.md) |
| `reconcile_fills.py` | 엔진의 `_reconcile_loop` 와 **같은 로직**을 1회 수동 실행. 접수불명 주문에 임의 재주문은 하지 않는다 |
| `precompute_panic_series.py` | pykrx 가 날짜쌍 단위 조회만 지원해 브레드스 계산이 느리다 — 미리 계산해 캐시 |

## 검증 결과를 보고할 때

- 수치뿐 아니라 **가정과 한계**를 함께 적는다.
- **돌리지 않은 검증은 "미검증"이라고 정직하게 적는다.** 통과했다고 추정하지 않는다.
