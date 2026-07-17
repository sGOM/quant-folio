import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DsrGradeBadge, DSR_GRADE_STYLE } from "@/components/DsrGradeBadge";
import { DsrAnalysis } from "@/lib/api";

describe("DsrGradeBadge", () => {
  const grades: DsrAnalysis["grade"][] = [
    "strong",
    "marginal",
    "inconclusive",
    "overfit_suspected",
    "insufficient_trials",
  ];

  it.each(grades)("등급 '%s'는 정의된 라벨·색상 클래스로 렌더된다", (grade) => {
    render(<DsrGradeBadge grade={grade} />);
    const style = DSR_GRADE_STYLE[grade];
    const badge = screen.getByText(style.label);
    for (const cls of style.className.split(" ")) {
      expect(badge.className).toContain(cls);
    }
  });

  it("강한 근거(strong)는 emerald 계열, 과최적화 의심은 red 계열이다", () => {
    const { unmount } = render(<DsrGradeBadge grade="strong" />);
    expect(screen.getByText(/강함/).className).toContain("emerald");
    unmount();

    render(<DsrGradeBadge grade="overfit_suspected" />);
    expect(screen.getByText(/과최적화 의심/).className).toContain("red");
  });

  it("알 수 없는 등급 값이 와도 시행 부족 스타일로 안전하게 폴백한다", () => {
    // 백엔드 스키마 밖의 값이 들어와도(타입 캐스팅 등) 렌더가 깨지지 않아야 한다.
    render(<DsrGradeBadge grade={"unknown_grade" as DsrAnalysis["grade"]} />);
    expect(screen.getByText("시행 부족")).toBeInTheDocument();
  });
});
