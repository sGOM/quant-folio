"""등록 리밸런싱 전략(id=23/24) config 에 P0-1 체결 필드를 명시 저장한다.

config.get 기본값(next_close·5bp)과 동일한 값을 명시해, 재실행 시 동작이 config 만
보고도 자명하게 한다(암묵적 기본값 의존 제거). 이미 값이 있으면 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import AsyncSessionLocal
from app.models import Strategy
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

DEFAULTS = {"fill_mode": "next_close", "slippage_bps": 5.0, "slippage_vol_scale": 0.0}
TARGET_IDS = [23, 24]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        for sid in TARGET_IDS:
            s = await db.scalar(select(Strategy).where(Strategy.id == sid))
            if s is None:
                print(f"id={sid}: 없음(스킵)", flush=True)
                continue
            cfg = dict(s.config)
            if cfg.get("type") != "rebalance":
                print(f"id={sid}: type={cfg.get('type')} (리밸런싱 아님, 스킵)", flush=True)
                continue
            added = {k: v for k, v in DEFAULTS.items() if k not in cfg}
            if not added:
                print(f"id={sid}: 이미 체결 필드 존재(변경 없음) — "
                      f"{ {k: cfg.get(k) for k in DEFAULTS} }", flush=True)
                continue
            cfg.update(added)
            s.config = cfg
            flag_modified(s, "config")
            print(f"id={sid} '{s.name}': 추가 {added}", flush=True)
        await db.commit()
    print("완료.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
