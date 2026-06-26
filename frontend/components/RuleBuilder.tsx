"use client";

import {
  Combinator,
  CompareOp,
  Condition,
  ConditionGroup,
  Operand,
  OperandKind,
  RuleNode,
  isGroup,
} from "@/lib/api";
import {
  COMPARE_OP_LABELS,
  OPERAND_KIND_LABELS,
  PRICE_SOURCE_LABELS,
} from "@/lib/strategy";

/** 입력/셀렉트 공용 스타일(StrategyForm 과 동일). */
const INPUT =
  "w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:border-ring";

const KINDS: OperandKind[] = [
  "price",
  "const",
  "sma",
  "ema",
  "rsi",
  "macd_line",
  "macd_signal",
];
const OPS: CompareOp[] = [">", "<", ">=", "<="];
const SOURCES = ["close", "open", "high", "low", "volume"] as const;

/** MACD 계열 피연산자의 3개 입력칸 설명(어떤 값을 넣는 칸인지 라벨로 표시). */
const MACD_FIELDS = [
  { key: "fast" as const, label: "단기 EMA", min: 1 },
  { key: "slow" as const, label: "장기 EMA", min: 2 },
  { key: "signal" as const, label: "시그널", min: 1 },
];

/** 종류 변경 시 적용할 피연산자 기본값. */
function defaultOperand(kind: OperandKind): Operand {
  switch (kind) {
    case "price":
      return { kind, source: "close" };
    case "const":
      return { kind, value: 0 };
    case "sma":
    case "ema":
      return { kind, period: 20 };
    case "rsi":
      return { kind, period: 14 };
    case "macd_line":
    case "macd_signal":
      return { kind, fast: 12, slow: 26, signal: 9 };
  }
}

/** 빈(초기) 조건. */
export function emptyCondition(): Condition {
  return {
    left: { kind: "sma", period: 5 },
    op: ">",
    right: { kind: "sma", period: 20 },
  };
}

/** 빈(초기) 하위 그룹(조건 1개 포함). */
function emptyGroup(combinator: Combinator = "or"): ConditionGroup {
  return { combinator, children: [emptyCondition()] };
}

/** 작은 숫자 입력. */
function NumInput({
  value,
  onChange,
  min,
  placeholder,
}: {
  value: number | null | undefined;
  onChange: (v: number) => void;
  min?: number;
  placeholder?: string;
}) {
  return (
    <input
      type="number"
      min={min}
      placeholder={placeholder}
      value={value === null || value === undefined ? "" : value}
      onChange={(e) => onChange(Number(e.target.value))}
      className={INPUT}
    />
  );
}

/** 라벨이 위에 붙은 작은 숫자 입력(어떤 값을 넣는 칸인지 명시). */
function LabeledNum({
  label,
  value,
  onChange,
  min,
}: {
  label: string;
  value: number | null | undefined;
  onChange: (v: number) => void;
  min?: number;
}) {
  return (
    <label className="block space-y-0.5">
      <span className="block text-[10px] leading-none text-muted-foreground">{label}</span>
      <NumInput value={value} min={min} onChange={onChange} />
    </label>
  );
}

/** 단일 피연산자 편집기(종류 드롭다운 + 종류별 입력). */
function OperandEditor({
  operand,
  onChange,
}: {
  operand: Operand;
  onChange: (op: Operand) => void;
}) {
  const patch = (p: Partial<Operand>) => onChange({ ...operand, ...p });

  return (
    <div className="grid grid-cols-2 gap-1">
      <select
        value={operand.kind}
        onChange={(e) => onChange(defaultOperand(e.target.value as OperandKind))}
        className={INPUT}
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {OPERAND_KIND_LABELS[k]}
          </option>
        ))}
      </select>

      {operand.kind === "price" && (
        <select
          value={operand.source ?? "close"}
          onChange={(e) => patch({ source: e.target.value as Operand["source"] })}
          className={INPUT}
        >
          {SOURCES.map((s) => (
            <option key={s} value={s}>
              {PRICE_SOURCE_LABELS[s]}
            </option>
          ))}
        </select>
      )}

      {operand.kind === "const" && (
        <NumInput
          value={operand.value}
          onChange={(v) => patch({ value: v })}
          placeholder="값"
        />
      )}

      {(operand.kind === "sma" ||
        operand.kind === "ema" ||
        operand.kind === "rsi") && (
        <NumInput
          value={operand.period}
          min={1}
          onChange={(v) => patch({ period: v })}
          placeholder="기간"
        />
      )}

      {(operand.kind === "macd_line" || operand.kind === "macd_signal") && (
        <div className="col-span-2 grid grid-cols-3 gap-1">
          {MACD_FIELDS.map((f) => (
            <LabeledNum
              key={f.key}
              label={f.label}
              min={f.min}
              value={operand[f.key]}
              onChange={(v) => patch({ [f.key]: v })}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** 조건 한 줄: 좌 피연산자 · 연산자 · 우 피연산자 · 삭제. */
function ConditionRow({
  cond,
  onChange,
  onRemove,
  removable,
}: {
  cond: Condition;
  onChange: (c: Condition) => void;
  onRemove: () => void;
  removable: boolean;
}) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-border bg-muted/30 p-2">
      <div className="flex-1">
        <OperandEditor operand={cond.left} onChange={(left) => onChange({ ...cond, left })} />
      </div>
      <select
        value={cond.op}
        onChange={(e) => onChange({ ...cond, op: e.target.value as CompareOp })}
        className="mt-0 w-28 rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:border-ring"
      >
        {OPS.map((o) => (
          <option key={o} value={o}>
            {COMPARE_OP_LABELS[o]}
          </option>
        ))}
      </select>
      <div className="flex-1">
        <OperandEditor operand={cond.right} onChange={(right) => onChange({ ...cond, right })} />
      </div>
      <button
        type="button"
        onClick={onRemove}
        disabled={!removable}
        title={removable ? "삭제" : "최소 1개가 필요합니다"}
        className="mt-1 rounded-md border border-input px-2 py-1 text-sm text-muted-foreground hover:bg-accent disabled:opacity-30"
      >
        ✕
      </button>
    </div>
  );
}

/** AND/OR 결합자 토글(2개 이상 자식일 때 의미가 있음). */
function CombinatorToggle({
  value,
  onChange,
}: {
  value: Combinator;
  onChange: (c: Combinator) => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-md border border-input text-xs">
      {(["and", "or"] as Combinator[]).map((c) => (
        <button
          key={c}
          type="button"
          onClick={() => onChange(c)}
          className={
            "px-2 py-1 " +
            (value === c
              ? "bg-primary text-white"
              : "bg-background text-muted-foreground hover:bg-accent")
          }
        >
          {c === "and" ? "그리고(AND)" : "또는(OR)"}
        </button>
      ))}
    </div>
  );
}

/**
 * 논리 그룹 편집기(재귀). 자식은 단일 조건이거나 다시 하위 그룹이며,
 * combinator(AND/OR)로 결합된다. 중첩으로 「(A 또는 B) 그리고 C」형 논리식을 만든다.
 *
 * @param removable 이 그룹을 통째로 삭제할 수 있는지(루트 그룹은 불가)
 * @param depth     중첩 깊이(들여쓰기·과도한 중첩 방지용)
 */
function GroupEditor({
  group,
  onChange,
  onRemove,
  removable,
  depth,
}: {
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
  onRemove?: () => void;
  removable: boolean;
  depth: number;
}) {
  const { children, combinator } = group;
  const setChild = (i: number, node: RuleNode) =>
    onChange({ ...group, children: children.map((c, j) => (j === i ? node : c)) });
  const removeChild = (i: number) =>
    onChange({ ...group, children: children.filter((_, j) => j !== i) });
  const addCondition = () =>
    onChange({ ...group, children: [...children, emptyCondition()] });
  const addGroup = () =>
    onChange({ ...group, children: [...children, emptyGroup(combinator === "and" ? "or" : "and")] });

  // 자식이 2개 이상일 때만 개별 자식 삭제 허용(그룹은 최소 1개 자식 유지).
  const childRemovable = children.length > 1;

  return (
    <div
      className={
        "space-y-2 rounded-md " +
        (depth > 0 ? "border border-dashed border-input bg-card/40 p-2" : "")
      }
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CombinatorToggle
            value={combinator}
            onChange={(c) => onChange({ ...group, combinator: c })}
          />
          <span className="text-[11px] text-muted-foreground">
            {combinator === "and" ? "모든 항목 충족 시" : "하나라도 충족 시"}
          </span>
        </div>
        {removable && onRemove && (
          <button
            type="button"
            onClick={onRemove}
            title="그룹 삭제"
            className="rounded-md border border-input px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
          >
            그룹 삭제 ✕
          </button>
        )}
      </div>

      {children.map((child, i) =>
        isGroup(child) ? (
          <GroupEditor
            key={i}
            group={child}
            depth={depth + 1}
            removable={childRemovable}
            onChange={(g) => setChild(i, g)}
            onRemove={() => removeChild(i)}
          />
        ) : (
          <ConditionRow
            key={i}
            cond={child}
            removable={childRemovable}
            onChange={(c) => setChild(i, c)}
            onRemove={() => removeChild(i)}
          />
        ),
      )}

      <div className="flex gap-2">
        <button
          type="button"
          onClick={addCondition}
          className="rounded-md border border-input px-3 py-1 text-xs text-foreground hover:bg-accent"
        >
          + 조건 추가
        </button>
        {depth < 2 && (
          <button
            type="button"
            onClick={addGroup}
            className="rounded-md border border-input px-3 py-1 text-xs text-foreground hover:bg-accent"
          >
            + 하위 그룹(괄호) 추가
          </button>
        )}
      </div>
    </div>
  );
}

/** 진입 또는 청산 논리식 그룹(루트). */
function RuleSection({
  title,
  group,
  onChange,
}: {
  title: string;
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
}) {
  return (
    <div className="space-y-2">
      <span className="text-xs font-medium text-foreground">{title}</span>
      <GroupEditor
        group={group}
        depth={0}
        removable={false}
        onChange={onChange}
      />
    </div>
  );
}

/**
 * 사용자 정의 전략의 진입·청산 규칙 편집기.
 * @param entry   진입 논리식(AND/OR 중첩 그룹)
 * @param exit    청산 논리식(AND/OR 중첩 그룹)
 * @param onChange 변경된 논리식을 필드별로 상위에 전달
 */
export function RuleBuilder({
  entry,
  exit,
  onChange,
}: {
  entry: ConditionGroup;
  exit: ConditionGroup;
  onChange: (field: "entry" | "exit", group: ConditionGroup) => void;
}) {
  return (
    <div className="space-y-4 rounded-md border border-border p-3">
      <RuleSection
        title="진입(매수) 조건"
        group={entry}
        onChange={(g) => onChange("entry", g)}
      />
      <div className="border-t border-border" />
      <RuleSection
        title="청산(매도) 조건"
        group={exit}
        onChange={(g) => onChange("exit", g)}
      />
      <p className="text-[11px] text-muted-foreground">
        ※ 조건이 거짓→참으로 바뀌는 순간을 신호로 봅니다. AND/OR 와 하위 그룹(괄호)을
        조합해 「(A 또는 B) 그리고 C」같은 논리식을 만들 수 있습니다.
      </p>
    </div>
  );
}
