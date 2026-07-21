import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { LineChart, OverlayLineChart } from "@/components/LineChart";

/** SVG 상수(LineChart.tsx 내부 W/pad 와 동일해야 좌표 계산을 재현할 수 있다). */
const W = 800;
const PAD = 8;

describe("LineChart", () => {
  it("NaN/Infinity 가 섞인 포인트는 걸러내고 유효한 값만으로 그린다", () => {
    const data = [
      { t: "2024-01-01", v: 100 },
      { t: "2024-01-02", v: NaN },
      { t: "2024-01-03", v: Infinity },
      { t: "2024-01-04", v: 110 },
      { t: "2024-01-05", v: -Infinity },
    ];
    const { container } = render(<LineChart data={data} />);
    // 유효 포인트 2개(100, 110)만 남아 path 가 정상 렌더된다(안내 문구가 아니라 svg).
    expect(container.querySelector("svg")).toBeInTheDocument();
    const linePath = container.querySelectorAll("path")[1]; // [0]=area, [1]=line
    // 좌표에 NaN 이 섞이면 path 문자열에 "NaN" 이 남는다 — 없어야 필터링이 성공한 것.
    expect(linePath?.getAttribute("d")).not.toContain("NaN");
    expect(linePath?.getAttribute("d")).toContain("M");
  });

  it("모든 값이 동일(span=0)해도 0 나눗셈 없이 수평선을 그린다", () => {
    const data = [
      { t: "2024-01-01", v: 50 },
      { t: "2024-01-02", v: 50 },
      { t: "2024-01-03", v: 50 },
    ];
    const { container } = render(<LineChart data={data} />);
    const linePath = container.querySelectorAll("path")[1];
    const d = linePath?.getAttribute("d") ?? "";
    expect(d).not.toContain("NaN");
    expect(d).not.toContain("Infinity");
    // span 이 1 로 보정되므로 v 가 모두 같으면 y 좌표도 모두 동일해야 한다.
    const ys = [...d.matchAll(/[ML] [\d.]+ ([\d.]+)/g)].map((m) => m[1]);
    expect(new Set(ys).size).toBe(1);
  });

  it("데이터 포인트가 2개 미만이면 안내 문구를 보여준다", () => {
    render(<LineChart data={[{ t: "2024-01-01", v: 100 }]} />);
    expect(screen.getByText("표시할 데이터가 없습니다.")).toBeInTheDocument();
  });

  it("데이터가 비어 있어도 안내 문구를 보여준다(빈 상태)", () => {
    render(<LineChart data={[]} />);
    expect(screen.getByText("표시할 데이터가 없습니다.")).toBeInTheDocument();
  });
});

describe("OverlayLineChart", () => {
  it("두 시리즈의 길이가 달라도 각자 인덱스 기준으로 x좌표를 독립 계산한다", () => {
    const a = Array.from({ length: 10 }, (_, i) => ({ t: `a-${i}`, v: i }));
    const b = Array.from({ length: 5 }, (_, i) => ({ t: `b-${i}`, v: i }));
    const { container } = render(
      <OverlayLineChart
        series={[
          { name: "A", color: "#111111", data: a },
          { name: "B", color: "#222222", data: b },
        ]}
      />,
    );
    const paths = container.querySelectorAll("path");
    expect(paths).toHaveLength(2);

    // 각 시리즈는 (자기 길이-1) 기준으로 x 를 나누므로, 마지막 포인트의 x 좌표는
    // 두 시리즈 모두 우측 끝(W - pad)에 도달한다 — 인덱스가 독립적으로 정규화된다는 뜻.
    const lastXOf = (d: string) => {
      const matches = [...d.matchAll(/[ML] ([\d.]+) [\d.]+/g)];
      return Number(matches[matches.length - 1][1]);
    };
    const rightEdge = W - PAD;
    expect(lastXOf(paths[0].getAttribute("d")!)).toBeCloseTo(rightEdge, 1);
    expect(lastXOf(paths[1].getAttribute("d")!)).toBeCloseTo(rightEdge, 1);

    // 두 번째 포인트(i=1)의 x 좌표는 시리즈 길이에 반비례해 달라진다(A: /9, B: /4).
    const secondXOf = (d: string) => {
      const matches = [...d.matchAll(/[ML] ([\d.]+) [\d.]+/g)];
      return Number(matches[1][1]);
    };
    const xA1 = secondXOf(paths[0].getAttribute("d")!);
    const xB1 = secondXOf(paths[1].getAttribute("d")!);
    expect(xB1).toBeGreaterThan(xA1); // B 가 포인트 수가 적어 같은 인덱스라도 더 오른쪽
  });

  it("모든 시리즈가 2개 미만이면 안내 문구를 보여준다", () => {
    render(
      <OverlayLineChart
        series={[{ name: "A", color: "#111111", data: [{ t: "x", v: 1 }] }]}
      />,
    );
    expect(screen.getByText("표시할 데이터가 없습니다.")).toBeInTheDocument();
  });
});
