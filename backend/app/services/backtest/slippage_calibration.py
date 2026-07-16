"""슬리피지 캘리브레이션 피드백 루프 (제안 산출).

체결 정합 실측(fill_quality.compute_fill_quality_report)의 M1(실행 슬리피지) 관측치를
근거로, 전략 config 의 `slippage_bps` 가정을 현실에 맞게 되먹일 "제안값"을 산출한다.

설계 원칙(안전):
- **자동 반영 금지.** 이 모듈은 제안(Proposal)만 만든다. config 에 실제로 쓰는 것은
  사람이 승인하는 별도 경로(app/api/routes/fill_quality.py 의 apply 라우트)뿐이다.
- **표본이 적으면 제안하지 않는다.** min_sample(기본 30) 미만이면 None — 모의투자 초기
  처럼 관측이 빈약할 때 잡음에 끌려 가정을 흔들지 않는다.
- **급변 방지(클램프).** 1회 조정폭을 현재값 대비 ±max_step_bps(기본 3bp)로 제한한다.
- **유의 변화만.** 클램프 후 제안값과 현재값 차이가 min_change_bps(기본 1bp) 미만이면
  변경할 가치가 없다고 보고 None.
- **robust 통계.** 평균이 아니라 **중앙값(median)** 을 관측 대표값으로 쓴다(이상 체결·
  부분체결 튐에 강건). 리포트의 m1_exec.all.median 을 그대로 읽는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.backtest.fill_quality import DEFAULT_MIN_SAMPLE, DEFAULT_SLIP_BPS

# 1회 조정폭 상한(bp) — 현재값 대비 ±이 값으로 클램프(급변 방지).
DEFAULT_MAX_STEP_BPS = 3.0
# 최소 유의 변화(bp) — 제안값이 현재값과 이보다 덜 다르면 제안 생략.
DEFAULT_MIN_CHANGE_BPS = 1.0


@dataclass(frozen=True)
class CalibrationProposal:
    """슬리피지 캘리브레이션 제안(사람 승인 전의 후보값).

    :param current_bps: 현재 전략 config 의 slippage_bps 가정.
    :param proposed_bps: 클램프·반올림 후 제안값(config 에 쓸 후보).
    :param observed_median_bps: 관측된 M1(실행 슬리피지) 편도 중앙값.
    :param sample_size: 제안 근거가 된 체결 표본 수(M1 all n).
    :param clamped: 클램프(±max_step)가 실제로 걸렸는지 여부.
    :param reason: 제안 사유(사람 승인 UI·알림 문구용).
    """

    current_bps: float
    proposed_bps: float
    observed_median_bps: float
    sample_size: int
    clamped: bool
    reason: str


def propose_slippage_calibration(
    report: dict[str, Any],
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    max_step_bps: float = DEFAULT_MAX_STEP_BPS,
    min_change_bps: float = DEFAULT_MIN_CHANGE_BPS,
) -> CalibrationProposal | None:
    """정합 리포트에서 slippage_bps 캘리브레이션 제안을 산출한다.

    :param report: compute_fill_quality_report / compute_fill_quality 의 반환 dict.
        m1_exec.all.{n,median} 와 assumptions.backtest_slip_bps 를 읽는다.
    :return: 제안(CalibrationProposal). 아래 중 하나면 None(제안 안 함):
        - M1 표본 수 < min_sample (표본 부족)
        - 관측 중앙값이 없음(계산 불가)
        - 클램프 후 제안값과 현재값 차이가 min_change_bps 미만(유의하지 않음)
    """
    m1_all = (report.get("m1_exec") or {}).get("all") or {}
    n = int(m1_all.get("n") or 0)
    median = m1_all.get("median")

    current = (report.get("assumptions") or {}).get("backtest_slip_bps")
    current_bps = float(current) if current is not None else DEFAULT_SLIP_BPS

    # 표본 부족 또는 관측 중앙값 부재 → 제안하지 않는다.
    if n < min_sample or median is None:
        return None

    observed = float(median)

    # 급변 방지: 현재값 대비 ±max_step_bps 로 클램프. 슬리피지는 음수가 될 수 없으므로 0 하한.
    low = current_bps - max_step_bps
    high = current_bps + max_step_bps
    clamped_val = min(max(observed, low), high)
    clamped_val = max(0.0, clamped_val)
    proposed_bps = round(clamped_val, 2)
    was_clamped = observed < low or observed > high

    # 유의하지 않은 변화(현재값과 차이 < min_change_bps) → 제안 생략.
    if abs(proposed_bps - current_bps) < min_change_bps:
        return None

    direction = "상향" if proposed_bps > current_bps else "하향"
    reason = (
        f"실측 실행 슬리피지 중앙값 {observed:.2f}bp(표본 {n}) 기준 "
        f"{current_bps:.2f}bp → {proposed_bps:.2f}bp {direction}"
        + (f" (±{max_step_bps:.0f}bp 클램프 적용)" if was_clamped else "")
    )
    return CalibrationProposal(
        current_bps=current_bps,
        proposed_bps=proposed_bps,
        observed_median_bps=observed,
        sample_size=n,
        clamped=was_clamped,
        reason=reason,
    )
