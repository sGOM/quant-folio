"""리밸런싱 백테스트 재현성 — 같은 입력이면 항상 같은 수치가 나와야 한다.

`_apply_rebalance` 가 주문을 만들 때 `set(targets) | set(val)` 을 그대로 순회하면
순서가 **종목 코드 문자열의 해시**에 좌우된다. CPython 은 프로세스마다
`PYTHONHASHSEED` 로 문자열 해시를 무작위화하므로, 그 순서가 sells/buys 리스트
순서가 되고 체결 루프가 `cash` 를 순차 누적하면서 같은 입력이 **실행마다 다른**
성과지표를 냈다(실측 ~1e-15, trades 배열 순서도 바뀐다). 미세하지만 둘을 망가뜨린다.

1. 백테스트 결과의 재현성 — 같은 전략을 두 번 돌리면 total_return 이 미세하게 다르다.
2. 이 함수에 수치 회귀 테스트를 걸 수 없다 — 기대값을 못 박을 수가 없다.

같은 프로세스 안에서는 해시 시드가 고정이라 이 결함이 드러나지 않는다. 그래서
서브프로세스 두 개를 **서로 다른 PYTHONHASHSEED** 로 띄워 대조한다.
"""
import json
import os
import subprocess
import sys

# 자식 프로세스에서 돌릴 최소 백테스트 — 부모와 같은 코드를 임포트한다.
_CHILD = """
import json
import numpy as np
import pandas as pd
from app.services.backtest.portfolio import run_rebalance_backtest

codes = [f"{i:06d}" for i in range(1, 21)]
dates = pd.bdate_range("2024-01-01", periods=200)
rng = np.random.default_rng(7)
steps = rng.normal(0.0005, 0.02, size=(len(dates), len(codes)))
panel = pd.DataFrame((10000 * np.exp(np.cumsum(steps, axis=0))).round(0),
                     index=dates, columns=codes)
cfg = {
    "universe": codes,
    "capital": 100_000_000,
    "selection": {"method": "momentum", "lookback": 20, "top_n": 5},
    "rebalance": {"cadence": "monthly"},
    "drift_band_pct": 0.02,
    "fill_mode": "same_close",
    "slippage_bps": 5.0,
}
res = run_rebalance_backtest(panel, cfg, dates[30], dates[-1])
print(json.dumps({
    "scalars": {k: res[k] for k in
                ("total_return", "cagr", "mdd", "sharpe", "num_trades", "avg_turnover")},
    "trades": [(t["t"], t["symbol"], t["side"], t["amount"]) for t in res["trades"]],
}))
"""


def _run(hash_seed: str) -> dict:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    out = subprocess.run(
        [sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, f"자식 프로세스 실패:\n{out.stderr}"
    return json.loads(out.stdout)


def test_해시시드가_달라도_백테스트_수치가_같다():
    """재현성 회귀 가드 — `sorted()` 가 빠지면 두 시드의 결과가 갈린다."""
    a = _run("0")
    b = _run("12345")

    assert a["scalars"]["num_trades"] > 0, "체결이 없으면 순서 의존을 검증할 수 없다"
    assert a["scalars"] == b["scalars"], (
        f"성과지표가 해시 시드에 따라 달라진다:\n{a['scalars']}\n{b['scalars']}"
    )
    assert a["trades"] == b["trades"], "체결 내역(순서·금액)이 해시 시드에 따라 달라진다"
