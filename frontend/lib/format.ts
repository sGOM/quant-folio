/** 금융 수치 표시용 포맷 유틸. 자릿수·부호·통화/퍼센트 표기를 일관되게 유지한다. */

/** 원화 금액. 예: 1234567 → "1,234,567원" */
export function formatKRW(value: number, withUnit = true): string {
  const s = new Intl.NumberFormat("ko-KR").format(Math.round(value));
  return withUnit ? `${s}원` : s;
}

/** 일반 정수/실수 천단위 구분. */
export function formatNumber(value: number, fractionDigits = 0): string {
  return new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

/** 퍼센트. 부호를 항상 표기한다. 예: 0.0123(비율) → "+1.23%". */
export function formatPercent(ratio: number, fractionDigits = 2): string {
  const sign = ratio > 0 ? "+" : "";
  return `${sign}${(ratio * 100).toFixed(fractionDigits)}%`;
}

/** 손익 부호. 색상 클래스/아이콘 선택에 사용한다. */
export type TrendSign = "up" | "down" | "flat";

export function trendOf(value: number): TrendSign {
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

/** 손익 부호별 텍스트 색상 클래스(색상에만 의존하지 않도록 부호와 병행). */
export function trendColor(value: number): string {
  const t = trendOf(value);
  return t === "up"
    ? "text-profit"
    : t === "down"
      ? "text-loss"
      : "text-neutral-trend";
}
