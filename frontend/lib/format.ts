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
const TREND_CLASS: Record<TrendSign, string> = {
  up: "text-profit",
  down: "text-loss",
  flat: "text-neutral-trend",
};

export function trendColor(value: number): string {
  return TREND_CLASS[trendOf(value)];
}

// ─────────────────── null 허용 표시 헬퍼(지표 테이블 공용) ───────────────────

/** null 가능한 소수 비율을 부호 포함 퍼센트로. null → "-". */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  return v == null ? "-" : formatPercent(v, digits);
}

/** null 가능한 숫자를 고정 소수 자릿수로. null → "-". */
export function fmtNum(v: number | null | undefined, digits = 1): string {
  return v == null ? "-" : v.toFixed(digits);
}

/** 원화 금액을 조/억/만 단위로 축약. null → "-". */
export function fmtAmt(v: number | null | undefined): string {
  if (v == null) return "-";
  const uk = v / 100_000_000; // 억
  if (uk >= 10_000) return `${(uk / 10_000).toFixed(1)}조`;
  if (uk >= 1) return `${Math.round(uk).toLocaleString("ko-KR")}억`;
  return `${Math.round(v / 10_000).toLocaleString("ko-KR")}만`;
}

/** null 가능한 비율에 대한 손익 색상 클래스. null → muted. */
export function pctColor(v: number | null | undefined): string {
  return v == null ? "text-muted-foreground" : trendColor(v);
}
