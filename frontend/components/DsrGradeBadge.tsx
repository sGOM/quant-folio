import { DsrAnalysis } from "@/lib/api";

/** DSR 등급 → (배지 라벨, 색상 클래스). */
export const DSR_GRADE_STYLE: Record<
  string,
  { label: string; className: string }
> = {
  strong: {
    label: "강함(≥0.95)",
    className:
      "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  },
  marginal: {
    label: "경계(≥0.90)",
    className:
      "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
  },
  inconclusive: {
    label: "불확실(≥0.50)",
    className:
      "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300",
  },
  overfit_suspected: {
    label: "과최적화 의심(<0.50)",
    className: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  },
  insufficient_trials: {
    label: "시행 부족",
    className: "bg-muted text-muted-foreground",
  },
};

/**
 * DSR(Deflated Sharpe Ratio) 등급 배지. 등급별 라벨·색상은 {@link DSR_GRADE_STYLE}
 * 참조. 알 수 없는 등급이 오면 "시행 부족" 스타일로 안전하게 폴백한다.
 * @param grade DsrAnalysis.grade
 */
export function DsrGradeBadge({ grade }: { grade: DsrAnalysis["grade"] }) {
  const style = DSR_GRADE_STYLE[grade] ?? DSR_GRADE_STYLE.insufficient_trials;
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}
    >
      {style.label}
    </span>
  );
}
