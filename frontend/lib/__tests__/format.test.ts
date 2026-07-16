import { describe, it, expect, vi, afterEach } from "vitest";
import {
  formatKRW,
  formatNumber,
  formatPercent,
  trendOf,
  trendColor,
  fmtPct,
  fmtNum,
  fmtAmt,
  pctColor,
  formatRelativeTime,
} from "@/lib/format";

describe("formatKRW", () => {
  it("천단위 콤마와 원 단위를 붙인다", () => {
    expect(formatKRW(1234567)).toBe("1,234,567원");
  });

  it("withUnit=false면 단위를 생략한다", () => {
    expect(formatKRW(1234567, false)).toBe("1,234,567");
  });

  it("소수는 반올림한다", () => {
    expect(formatKRW(999.6)).toBe("1,000원");
  });
});

describe("formatNumber", () => {
  it("기본 자릿수는 정수 표시", () => {
    expect(formatNumber(1234.5678)).toBe("1,235");
  });

  it("fractionDigits 지정 시 소수 자릿수를 맞춘다", () => {
    expect(formatNumber(1234.5, 2)).toBe("1,234.50");
  });
});

describe("formatPercent", () => {
  it("양수에는 + 부호를 붙인다", () => {
    expect(formatPercent(0.0123)).toBe("+1.23%");
  });

  it("음수는 - 부호가 자연히 붙는다", () => {
    expect(formatPercent(-0.05)).toBe("-5.00%");
  });

  it("0은 부호 없이 표시한다", () => {
    expect(formatPercent(0)).toBe("0.00%");
  });

  it("fractionDigits로 소수 자릿수를 조정한다", () => {
    expect(formatPercent(0.012345, 3)).toBe("+1.234%"); // toFixed 반올림 결과 확인용
  });
});

describe("trendOf / trendColor", () => {
  it("양수는 up, 음수는 down, 0은 flat", () => {
    expect(trendOf(1)).toBe("up");
    expect(trendOf(-1)).toBe("down");
    expect(trendOf(0)).toBe("flat");
  });

  it("부호별 색상 클래스를 반환한다", () => {
    expect(trendColor(10)).toBe("text-profit");
    expect(trendColor(-10)).toBe("text-loss");
    expect(trendColor(0)).toBe("text-neutral-trend");
  });
});

describe("fmtPct / fmtNum", () => {
  it("null/undefined는 '-'를 반환한다", () => {
    expect(fmtPct(null)).toBe("-");
    expect(fmtPct(undefined)).toBe("-");
    expect(fmtNum(null)).toBe("-");
    expect(fmtNum(undefined)).toBe("-");
  });

  it("값이 있으면 formatPercent/toFixed를 사용한다", () => {
    expect(fmtPct(0.0567)).toBe("+5.7%");
    expect(fmtNum(3.14159, 2)).toBe("3.14");
  });
});

describe("fmtAmt", () => {
  it("null/undefined는 '-'를 반환한다", () => {
    expect(fmtAmt(null)).toBe("-");
    expect(fmtAmt(undefined)).toBe("-");
  });

  it("1억 미만은 만 단위로 표시한다", () => {
    expect(fmtAmt(50_000_000)).toBe("5,000만");
    expect(fmtAmt(0)).toBe("0만");
  });

  it("1억 이상 1조 미만은 억 단위로 표시한다(경계값 포함)", () => {
    expect(fmtAmt(100_000_000)).toBe("1억"); // 정확히 1억(경계)
    expect(fmtAmt(999_999_999)).toBe("10억"); // 반올림 경계
  });

  it("1조 이상은 조 단위로 소수 첫째자리까지 표시한다(경계값 포함)", () => {
    expect(fmtAmt(1_000_000_000_000)).toBe("1.0조"); // 정확히 1조(경계)
    expect(fmtAmt(1_500_000_000_000)).toBe("1.5조");
  });
});

describe("pctColor", () => {
  it("null/undefined는 muted 클래스를 반환한다", () => {
    expect(pctColor(null)).toBe("text-muted-foreground");
    expect(pctColor(undefined)).toBe("text-muted-foreground");
  });

  it("값이 있으면 trendColor를 위임 호출한다", () => {
    expect(pctColor(1)).toBe("text-profit");
    expect(pctColor(-1)).toBe("text-loss");
  });
});

describe("formatRelativeTime", () => {
  const NOW = new Date("2026-07-17T12:00:00.000Z").getTime();

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("null/undefined/파싱 불가 문자열은 '-'를 반환한다", () => {
    expect(formatRelativeTime(null)).toBe("-");
    expect(formatRelativeTime(undefined)).toBe("-");
    expect(formatRelativeTime("not-a-date")).toBe("-");
  });

  it("5초 미만 차이는 '방금 전'", () => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    const iso = new Date(NOW - 3_000).toISOString();
    expect(formatRelativeTime(iso)).toBe("방금 전");
  });

  it("1분 미만 차이는 'N초 전'", () => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    const iso = new Date(NOW - 42_000).toISOString();
    expect(formatRelativeTime(iso)).toBe("42초 전");
  });

  it("1시간 미만 차이는 'N분 전'", () => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    const iso = new Date(NOW - 5 * 60_000).toISOString();
    expect(formatRelativeTime(iso)).toBe("5분 전");
  });

  it("하루 미만 차이는 'N시간 전'", () => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    const iso = new Date(NOW - 3 * 3_600_000).toISOString();
    expect(formatRelativeTime(iso)).toBe("3시간 전");
  });

  it("하루 이상 차이는 'N일 전'", () => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    const iso = new Date(NOW - 2 * 86_400_000).toISOString();
    expect(formatRelativeTime(iso)).toBe("2일 전");
  });
});
