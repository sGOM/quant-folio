---
paths:
  - "backend/app/models/models.py"
  - "backend/alembic/**"
---

# 운영 테이블 (`app/models/models.py`)

사용자가 만들고 엔진이 채우는 데이터. 삭제 정책(`ondelete`)이 도메인 규칙을 담고 있다.

## 관계

```
users ──1:N── strategies ──1:N── backtests
  │               │  └─ featured_backtest_id ──▶ backtests (SET NULL, 대표 백테스트)
  │               │  └─ copied_from_id ──▶ strategies (SET NULL, 자기참조·복사 출처)
  │               ├──1:N── orders ──1:N── executions
  │               ├──1:N── positions
  │               └──N:M── users  (strategy_likes)
  ├──1:N── risk_limits
  ├──1:N── orders / positions / alerts
  └──1:N── strategy_likes

news_articles ──1:N── news_article_symbols
price_ticks  (독립, TimescaleDB hypertable)
```

## 테이블

| 테이블 | 핵심 컬럼 | 부모 | ondelete | 왜 그 정책인가 |
|---|---|---|---|---|
| `users` | id, email | — | — | 루트 |
| `strategies` | id, user_id, config(JSON), status | users | **CASCADE** | 사용자를 지우면 전략도 사라진다 |
| | `featured_backtest_id` | backtests | SET NULL | 대표 백테스트가 지워져도 전략은 산다 |
| | `copied_from_id` (자기참조) | strategies | SET NULL | 원본을 지워도 복사본은 산다 |
| `backtests` | id, strategy_id, 결과(JSON) | strategies | **CASCADE** | 전략에 종속된 산출물 |
| `strategy_likes` | strategy_id, user_id | strategies / users | CASCADE / CASCADE | 순수 연결 테이블 |
| `orders` | id, user_id, strategy_id, symbol, side, status, price, reason | users | **CASCADE** | |
| | | strategies | **SET NULL** | **전략을 지워도 체결 이력은 남긴다**(감사 추적) |
| `executions` | id, order_id, strategy_id, filled_price, qty | orders | CASCADE | 주문 없는 체결은 없다 |
| | | strategies | SET NULL | 위와 같은 이유 |
| `positions` | user_id, strategy_id, symbol, qty, avg_price | users / strategies | CASCADE / SET NULL | |
| `risk_limits` | user_id, strategy_id, 한도들 | users / strategies | CASCADE / CASCADE | 전략별 한도 + 계정 공통 한도(strategy_id NULL) |
| `alerts` | user_id, strategy_id, severity, code, message | users | CASCADE(nullable) | `user_id=NULL` 은 전역 알림(배치 장애 등) |
| `price_ticks` | PK(time, symbol) + OHLCV | — | — | **TimescaleDB hypertable**(파티션 키 `time`) |
| `news_articles` | id, url(unique), published_at | — | — | url 기준 멱등, 60일 보존 |
| `news_article_symbols` | article_id, symbol | news_articles | CASCADE | |

## 규칙

- **주문/체결의 `strategy_id` 는 SET NULL 이다.** 전략 삭제가 체결 이력을 지우면 감사가 불가능해진다.
  이 컬럼을 조회할 땐 항상 `NULL` 가능성을 다뤄야 한다.
- **`orders.reason` 은 한국어 서술.** 어떤 신호·공식·리스크·리밸런싱 기준으로 그 주문이 나갔는지
  사람이 읽을 수 있게 남긴다. 새 주문 경로를 만들면 반드시 채운다.
- **`alerts.user_id = NULL` 은 전역 알림.** 배치 장애처럼 특정 사용자에 귀속되지 않는 사건.
  `code` 로 종류를 식별하고(`runner_failures`·`mdd_kill`·`db_backup_stale` 등) 중복 억제에 쓴다.
- **enum 은 `StrEnum`** (`StrategyStatus`·`OrderSide`·`OrderStatus`).
- **`strategies` ↔ `backtests` 는 FK 가 둘이라 모호하다**(`backtests.strategy_id`,
  `strategies.featured_backtest_id`). relationship 을 추가할 땐 `foreign_keys` 를 명시한다.
