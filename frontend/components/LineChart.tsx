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
  // NaN/Infinity 가 섞이면 min/max→span→path 전부 NaN 이 되어 SVG 가 조용히
  // 빈 화면이 된다(에러 없음). 유효하지 않은 포인트는 미리 걸러낸다.
  data = data.filter((d) => Number.isFinite(d.v));
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

/**
 * 의존성 없는 경량 SVG 오버레이 라인차트. 서로 다른 두 시계열(예: 실측 NAV 인덱스 ↔
 * 백테스트 기대 인덱스)을 같은 좌표계(공유 min/max)에 겹쳐 그리고, 색상 범례를 붙인다.
 * 각 시계열은 값 배열 개수만 맞으면 되고(x 축은 인덱스 위치 기준), 날짜 정합은
 * 호출부(`lib/tracking.ts` 등)에서 미리 맞춰 넘겨야 한다.
 * @param series 렌더할 시계열들({ name, color, data })
 * @param height 차트 높이(px). 기본 240
 */
export function OverlayLineChart({
  series,
  height = 240,
}: {
  series: { name: string; color: string; data: { t: string; v: number }[] }[];
  height?: number;
}) {
  const cleaned = series
    .map((s) => ({ ...s, data: s.data.filter((d) => Number.isFinite(d.v)) }))
    .filter((s) => s.data.length >= 2);

  if (cleaned.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-neutral-500">
        표시할 데이터가 없습니다.
      </div>
    );
  }

  const W = 800;
  const H = height;
  const pad = 8;
  const allVals = cleaned.flatMap((s) => s.data.map((d) => d.v));
  const min = Math.min(...allVals);
  const max = Math.max(...allVals);
  const span = max - min || 1;

  const y = (v: number) => H - pad - ((v - min) / span) * (H - 2 * pad);

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        preserveAspectRatio="none"
        style={{ height }}
      >
        {cleaned.map((s) => {
          const n = s.data.length;
          const x = (i: number) => pad + (i / (n - 1)) * (W - 2 * pad);
          const path = s.data
            .map(
              (d, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(d.v).toFixed(1)}`,
            )
            .join(" ");
          return (
            <path
              key={s.name}
              d={path}
              fill="none"
              stroke={s.color}
              strokeWidth={1.5}
            />
          );
        })}
      </svg>
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
        {cleaned.map((s) => (
          <span key={s.name} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: s.color }}
            />
            {s.name}
          </span>
        ))}
      </div>
    </div>
  );
}
