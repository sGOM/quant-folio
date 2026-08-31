# 알림(alerts) 체계

무인 자동매매의 **"조용한 실패"를 사용자에게 알리는** 경로. engine·worker·web·frontend 4곳에 걸친다.

## 발행 → 도달

`engine/alerts.py::publish_alert` 하나가 진입점이고, **세 경로로 동시에** 나간다.

```
publish_alert(redis, user_id, strategy_id, severity, message, code, dedup_window_hours)
   ├─▶ Redis engine:events:{user_id} ──▶ web(ws.py) ──▶ 프론트 WS 토스트   (순간 전송)
   ├─▶ alerts 테이블 적재                ──▶ GET /api/alerts ──▶ AlertCenter  (영속)
   └─▶ 텔레그램 봇  (severity="critical" 만)                                (앱 미접속 대비)
```

- **WS·텔레그램은 순간 전송이라 유실된다** — 미접속 중이거나 warning 이면 놓친다. 그래서 **모든 알림을 DB에도 적재**한다(§17). 영속화 실패는 로그만 남기고 WS·텔레그램 흐름을 막지 않는다.
- 텔레그램은 `TELEGRAM_BOT_TOKEN`/`CHAT_ID` 미설정 시 자동 비활성.
- `user_id=None` 은 **전역 알림**(배치 장애 등, 특정 사용자에 귀속되지 않음).

## 중복 억제 (`dedup_window_hours`, §21)

`db_backup_stale` 처럼 **원인이 해소될 때까지 매 beat 주기마다 재발행되는 상태형 경보**는
테이블을 증식시키고 알림 피로를 만든다.

- 지정하면 같은 `(user_id, strategy_id, code)` 의 **미확인(`is_read=False`)** 알림이 그 시간 안에 이미 있으면 **DB 적재·텔레그램 재발송을 건너뛴다.**
- **WS 토스트는 dedup 과 무관하게 항상 통과** — 실시간성은 유지한다.
- `None`(기본)이면 매번 적재+발송.

## 보존정책 (`worker.cleanup_old_alerts`, 매일 04:00 KST)

| 대상 | 보존 |
|---|---|
| 확인(read) 알림 | **90일** |
| 미확인(unread) 알림 | **180일** — 아직 못 봤을 수 있으므로 더 오래 |

## `code` 레지스트리

`code` 는 프론트의 dedup·필터와 중복 억제 키에 쓰인다. **새 알림을 만들면 여기에 추가한다.**

| code | 발행처 | 무엇이 잘못됐나 |
|---|---|---|
| `runner_failures` | engine(`base_runner`·`main`) | 러너 연속 실패가 `FAILURE_ALERT_THRESHOLD`(3) 도달 |
| `order_unconfirmed` | engine(`executor`·`reconcile`) | 주문 접수 여부 불명(네트워크 예외) — 사람 확인 필요 |
| `oversell_clamped` | engine(`fills`) | 보유 초과 매도를 클램프함(§35) |
| `mdd_kill` | engine(`rebalance_runner`) | MDD 킬스위치 발동 |
| `price_feed_outage` | engine(`price_feed`) | 실시간 시세 WS 재연결 실패(§25) |
| `live_gate_blocked` | engine(`base_runner`·`main`) | 실전 전환 게이트가 전략 기동을 차단 |
| `pit_fallback` | engine(`rebalance_runner`) | PIT 유니버스를 못 얻어 폴백 |
| `factor_outage` | engine(`rebalance_runner`) | 팩터 조회 전면 장애 |
| `panic_overlay_unsupported` | engine(`rebalance_runner`) | 패닉 오버레이 전제 미충족 |
| `sector_map_outage` | worker | 업종분류 스냅샷 실패 |
| `kis_master_outage` | worker | KIS 종목마스터 적재 실패 |
| `ohlcv_ingest_failure_rate` | worker | 일봉 적재 실패율 초과 |
| `snapshot_ingest_failure_rate` | worker | 로컬 저장소 선적재 실패율 초과 |
| `news_ingest_failure` | worker | 뉴스 RSS 전체 실패 |
| `db_backup_failed` / `db_backup_stale` / `db_backup_s3_upload_failed` | worker | 백업 실패 / 신선도 초과 / S3 업로드 실패 |
| `alert_cleanup_failed` | worker | 알림 정리 배치 실패 |
| `fill_quality_drift` | worker | 슬리피지 실측 드리프트 |
| `slippage_calibration_proposed` | worker | 슬리피지 재보정 제안 |

## 새 배치·루프를 만들 때

**실패 경로에 `publish_alert(code=...)` 를 반드시 붙인다.** 무인 운용이라 로그만 남기면
아무도 모른다. 상태형(원인 해소까지 반복) 경보면 `dedup_window_hours` 를 함께 지정한다.

## 테스트 주의

알림 테스트가 **실 DB `alerts` 테이블을 오염시킨 전례**가 있다. 차단은 개별 테스트가 아니라
파일 전체 autouse 픽스처로 건다(`tests/test_worker_snapshots.py::_isolate_alert_publishing` 표본).
자세한 규칙은 `docs/CONVENTIONS.md` §1 "테스트 격리".
