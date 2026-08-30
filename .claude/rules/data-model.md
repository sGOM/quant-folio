# 데이터 모델 — 전체 지도

테이블은 **두 덩어리**로 갈린다. 소유 도메인이 다르고 수명주기·삭제 정책도 다르다.

| 덩어리 | 정의 파일 | 성격 | 상세 |
|---|---|---|---|
| **운영(trading)** | `app/models/models.py` | 사용자가 만든 것 — 전략·주문·체결·포지션 | [data-model/trading.md](data-model/trading.md) |
| **시장데이터 저장소(store)** | `app/models/store.py` | 외부에서 받아 굳힌 확정 과거 데이터 | [data-model/market-store.md](data-model/market-store.md) |

## 도메인 경계

```
┌─────────────────── 운영 (models.py) ───────────────────┐
│  users ─┬─ strategies ─┬─ backtests                    │
│         │              ├─ orders ── executions         │
│         │              ├─ positions                    │
│         │              └─ strategy_likes               │
│         ├─ risk_limits                                 │
│         └─ alerts                                      │
│  news_articles ── news_article_symbols                 │
│  price_ticks (TimescaleDB hypertable)                  │
└────────────────────────────────────────────────────────┘
                          │ FK 없음. symbol(6자리 코드) 문자열로만 느슨히 연결
                          ▼
┌────────────── 시장데이터 저장소 (store.py) ──────────────┐
│  stock_daily_snapshots   stock_period_stats            │
│  index_ohlcv ── index_ohlcv_coverage                   │
│  index_constituents      dart_financials               │
│  external_fetches  ← 적재 원장(모든 위 테이블의 상태 기록) │
│  sector_map_snapshots    kis_stock_master_snapshots    │
└────────────────────────────────────────────────────────┘
```

**두 덩어리 사이에 FK 는 없다.** 운영 테이블의 `symbol` 과 저장소의 `symbol` 은 같은
6자리 KRX 종목코드지만 참조 제약을 걸지 않는다 — 저장소는 언제든 통째로 지우고 다시
적재할 수 있어야 하고(강제 재적재), 운영 데이터가 거기 묶이면 안 된다.

## 마이그레이션

Alembic. 현재 head `0017`.

```bash
docker compose exec web alembic upgrade head
docker compose exec web alembic revision --autogenerate -m "<msg>"
```
