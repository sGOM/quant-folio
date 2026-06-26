"use client";

/**
 * 의존성 없는 경량 SVG 라인차트(백테스트 equity curve 용).
 * 데이터 포인트가 2개 미만이면 안내 문구를 렌더한다.
 * @param data   시계열 포인트 배열({ t: 시각, v: 값 })
 * @param height 차트 높이(px). 기본 240
 * @param color  선/면 색상. 기본 파란색(#3b82f6)
 */
export function LineChart({
  data,
  height = 240,
  color = "#3b82f6",
}: {
  data: { t: string; v: number }[];
  height?: number;
  color?: string;
}) {
  if (data.length < 2) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-neutral-500">
        표시할 데이터가 없습니다.
      </div>
    );
  }

  // SVG viewBox 좌표계 상수: 고정 폭/높이/여백으로 그린 뒤 CSS 로 가로 100% 늘인다.
  const W = 800;      // viewBox 가상 폭
  const H = height;   // viewBox 높이(= 렌더 높이)
  const pad = 8;      // 내부 여백(px)
  const vals = data.map((d) => d.v);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1; // 값 범위(0 이면 1 로 보정해 0 나눗셈 방지)

  /** 데이터 인덱스 i → SVG x 좌표 */
  const x = (i: number) => pad + (i / (data.length - 1)) * (W - 2 * pad);
  /** 값 v → SVG y 좌표(위가 0 이므로 상하 반전) */
  const y = (v: number) => H - pad - ((v - min) / span) * (H - 2 * pad);

  const path = data
    .map((d, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(d.v).toFixed(1)}`)
    .join(" ");
  const area = `${path} L ${x(data.length - 1).toFixed(1)} ${H - pad} L ${x(0).toFixed(1)} ${H - pad} Z`;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full"
      preserveAspectRatio="none"
      style={{ height }}
    >
      <path d={area} fill={color} opacity={0.12} />
      <path d={path} fill="none" stroke={color} strokeWidth={1.5} />
    </svg>
  );
}
