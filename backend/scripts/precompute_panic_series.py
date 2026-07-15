"""패닉 시계열(compute_panic_series) 사전계산 — 로컬 파일 캐시(.cache/panic_breadth_*.json)에 적재.

pykrx get_market_price_change 는 날짜쌍 단위 조회만 지원해 브레드스(하락종목비율 등)를
백테스트 구간 전체로 만들려면 거래일 수만큼 네트워크 호출이 필요하다(약 1.5~2초/거래일).
이 스크립트를 미리 실행해 캐시를 채워두면 이후 백테스트 비교 실행이 즉시(캐시 히트) 끝난다.
"""
from __future__ import annotations

import sys
import time
from datetime import date

sys.path.insert(0, "/app")

from app.core.config import settings  # noqa: F401  (KRX_ID/PW env 주입 트리거)
from app.services.metrics.panic import compute_panic_series

MARKET = "KOSPI"
# 표본 확충 재검증(코디네이터 지시): 2022 약세장·2024-08 엔캐리 청산 패닉 에피소드까지
# 포함하도록 최대한 넓게(2019-01~2025-06). 이미 캐시된 2019-11~2021-03 구간은
# compute_panic_series 가 자동으로 건너뛴다(증분).
START = date(2019, 1, 1)
END = date(2025, 6, 30)

if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    t0 = time.time()
    print(f"사전계산 시작: {MARKET} {START}~{END}", flush=True)
    df = compute_panic_series(MARKET, START, END, progress_every=20)
    print(f"완료: {len(df)}행, {time.time()-t0:.1f}초", flush=True)
    print(df[df["level"].isin(["warning", "panic"])], flush=True)
    print("PRECOMPUTE_DONE", flush=True)
