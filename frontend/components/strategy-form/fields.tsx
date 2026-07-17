"use client";

/**
 * 전략 폼 공용 입력 프리미티브 — StrategyForm 본체·RebalanceFields·RuleBuilder 가 공유한다.
 */

/**
 * 입력/셀렉트 공용 스타일(shadcn input 토큰).
 * bg-transparent 대신 bg-input 을 명시해 네이티브 select 가 OS 기본(흰 배경)
 * 콤보박스로 렌더되는 것을 방지하고, text-foreground 로 글씨 대비를 보장한다.
 */
export const INPUT =
  "flex h-9 w-full rounded-md border border-input bg-input text-foreground px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

/** 라벨이 달린 폼 필드 래퍼. */
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

/** 숫자 입력 필드(라벨 포함). */
export function NumField({
  label,
  value,
  onChange,
  min,
  max,
  step,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(Number(e.target.value))}
        className={INPUT}
      />
    </Field>
  );
}

/** 퍼센트 입력 필드(빈 값 허용 = 비활성). */
export function PctField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={0}
        max={100}
        step={0.1}
        placeholder="—"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT}
      />
    </Field>
  );
}

/** 거래비용(수수료·세금) 퍼센트 입력 필드. 빈 값 = 0%. */
export function CostField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={0}
        max={1}
        step={0.001}
        placeholder="0"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT}
      />
    </Field>
  );
}
