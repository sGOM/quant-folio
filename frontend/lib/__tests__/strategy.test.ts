import { describe, it, expect } from "vitest";
import {
  summarizeConfig,
  formatNode,
  formatCondition,
  formatOperand,
  countConditions,
  flattenConditions,
  formatCustomFormula,
  defaultConfig,
} from "@/lib/strategy";
import type {
  Condition,
  ConditionGroup,
  SmaConfig,
  RsiConfig,
  MacdConfig,
  CustomConfig,
  RebalanceConfig,
} from "@/lib/api";

const BASE = {
  symbol: "005930",
  cash: 10_000_000,
  fees: 0.00015,
  tax: 0.002,
  stop_loss_pct: null,
  take_profit_pct: null,
  trailing_stop_pct: null,
  fill_mode: "next_close" as const,
  slippage_bps: 5,
  slippage_vol_scale: 0,
  risk_free_rate: 0,
};

describe("summarizeConfig", () => {
  it("sma_crossover: 종목 · 라벨 fast/slow", () => {
    const cfg: SmaConfig = { ...BASE, type: "sma_crossover", fast: 5, slow: 20 };
    expect(summarizeConfig(cfg)).toBe("005930 · SMA 크로스 5/20");
  });

  it("rsi: period (lower/upper)", () => {
    const cfg: RsiConfig = { ...BASE, type: "rsi", period: 14, lower: 30, upper: 70 };
    expect(summarizeConfig(cfg)).toBe("005930 · RSI 14 (30/70)");
  });

  it("macd: fast/slow/signal", () => {
    const cfg: MacdConfig = { ...BASE, type: "macd", fast: 12, slow: 26, signal: 9 };
    expect(summarizeConfig(cfg)).toBe("005930 · MACD 12/26/9");
  });

  it("리스크 청산 필드가 있으면 뒤에 덧붙인다", () => {
    const cfg: SmaConfig = {
      ...BASE,
      type: "sma_crossover",
      fast: 5,
      slow: 20,
      stop_loss_pct: 0.05,
      take_profit_pct: 0.1,
      trailing_stop_pct: 0.03,
    };
    expect(summarizeConfig(cfg)).toBe(
      "005930 · SMA 크로스 5/20 · 손절 5% · 익절 10% · 트레일링 3%",
    );
  });

  it("custom: 진입/청산 조건 개수를 표시한다", () => {
    const cfg: CustomConfig = {
      ...BASE,
      type: "custom",
      entry: {
        combinator: "and",
        children: [
          { left: { kind: "sma", period: 5 }, op: ">", right: { kind: "sma", period: 20 } },
        ],
      },
      exit: {
        combinator: "and",
        children: [
          { left: { kind: "sma", period: 5 }, op: "<", right: { kind: "sma", period: 20 } },
        ],
      },
    };
    expect(summarizeConfig(cfg)).toBe("005930 · 사용자 정의 진입 1개 · 청산 1개");
  });

  it("rebalance(momentum): 종목수·선정규칙·주기 요약", () => {
    const cfg: RebalanceConfig = {
      type: "rebalance",
      universe: ["005930", "000660", "035720"],
      selection: { method: "momentum", lookback: 120, top_n: 2 },
      weighting: "equal",
      cadence: "monthly",
      rebalance_weekday: null,
      rebalance_dom: 1,
      rebalance_time: "14:30",
      initial_fill_immediate: false,
      drift_band_pct: 0.05,
      regime_filter: null,
      capital: 10_000_000,
      fees: 0.00015,
      tax: 0.002,
      fill_mode: "next_close",
      slippage_bps: 5,
      slippage_vol_scale: 0,
      risk_free_rate: 0,
      benchmark_index: "KOSPI200",
    };
    expect(summarizeConfig(cfg)).toBe(
      "리밸런싱(포트폴리오) · 3종목 · 모멘텀 상위 2 · 월간 리밸런싱",
    );
  });

  it("rebalance(score): 멀티팩터 상위 요약", () => {
    const cfg: RebalanceConfig = {
      type: "rebalance",
      universe: ["005930", "000660"],
      selection: { method: "score", lookback: 120, top_n: 10 },
      weighting: "score",
      cadence: "quarterly",
      rebalance_weekday: null,
      rebalance_dom: 1,
      rebalance_time: "14:30",
      initial_fill_immediate: false,
      drift_band_pct: 0.05,
      regime_filter: null,
      capital: 10_000_000,
      fees: 0.00015,
      tax: 0.002,
    };
    expect(summarizeConfig(cfg)).toBe(
      "리밸런싱(포트폴리오) · 2종목 · 멀티팩터 상위 10 · 분기 리밸런싱",
    );
  });

  it("rebalance(all): 전체 동일비중 요약", () => {
    const cfg: RebalanceConfig = {
      type: "rebalance",
      universe: ["005930"],
      selection: { method: "all", lookback: 120, top_n: 1 },
      weighting: "equal",
      cadence: "weekly",
      rebalance_weekday: 0,
      rebalance_dom: null,
      rebalance_time: "14:30",
      initial_fill_immediate: false,
      drift_band_pct: 0.05,
      regime_filter: null,
      capital: 10_000_000,
      fees: 0.00015,
      tax: 0.002,
    };
    expect(summarizeConfig(cfg)).toBe(
      "리밸런싱(포트폴리오) · 1종목 · 전체 동일비중 · 주간 리밸런싱",
    );
  });

  it("rebalance: universe_rule 소스가 fixed 가 아니면 소스명을 표시한다(PIT)", () => {
    const cfg: RebalanceConfig = {
      type: "rebalance",
      universe: ["005930"],
      selection: {
        method: "momentum",
        lookback: 120,
        top_n: 5,
        universe_rule: { source: "KOSPI200" },
      },
      weighting: "equal",
      cadence: "monthly",
      rebalance_weekday: null,
      rebalance_dom: 1,
      rebalance_time: "14:30",
      initial_fill_immediate: false,
      drift_band_pct: 0.05,
      regime_filter: null,
      capital: 10_000_000,
      fees: 0.00015,
      tax: 0.002,
    };
    expect(summarizeConfig(cfg)).toBe(
      "리밸런싱(포트폴리오) · KOSPI200 · 모멘텀 상위 5 · 월간 리밸런싱",
    );
  });
});

describe("formatOperand / formatCondition", () => {
  it("price: source 라벨(기본 종가)", () => {
    expect(formatOperand({ kind: "price" })).toBe("종가");
    expect(formatOperand({ kind: "price", source: "volume" })).toBe("거래량");
  });

  it("const: 값을 문자열로", () => {
    expect(formatOperand({ kind: "const", value: 30 })).toBe("30");
    expect(formatOperand({ kind: "const", value: null })).toBe("?");
  });

  it("sma/ema/rsi: 라벨(기간)", () => {
    expect(formatOperand({ kind: "sma", period: 5 })).toBe("SMA(5)");
    expect(formatOperand({ kind: "ema", period: 12 })).toBe("EMA(12)");
    expect(formatOperand({ kind: "rsi", period: 14 })).toBe("RSI(14)");
  });

  it("macd_line/macd_signal", () => {
    expect(formatOperand({ kind: "macd_line", fast: 12, slow: 26, signal: 9 })).toBe(
      "MACD(12,26,9)",
    );
    expect(formatOperand({ kind: "macd_signal", fast: 12, slow: 26, signal: 9 })).toBe(
      "Signal(12,26,9)",
    );
  });

  it("formatCondition: 좌 op 우 결합", () => {
    const cond: Condition = {
      left: { kind: "sma", period: 5 },
      op: ">",
      right: { kind: "sma", period: 20 },
    };
    expect(formatCondition(cond)).toBe("SMA(5) > SMA(20)");
  });
});

describe("countConditions / flattenConditions", () => {
  it("단일 조건은 1개", () => {
    const cond: Condition = {
      left: { kind: "price" },
      op: ">",
      right: { kind: "const", value: 100 },
    };
    expect(countConditions(cond)).toBe(1);
    expect(flattenConditions(cond)).toEqual([cond]);
  });

  it("그룹 내 여러 조건을 평탄화한다", () => {
    const c1: Condition = { left: { kind: "rsi", period: 14 }, op: "<", right: { kind: "const", value: 30 } };
    const c2: Condition = { left: { kind: "sma", period: 5 }, op: ">", right: { kind: "sma", period: 20 } };
    const group: ConditionGroup = { combinator: "or", children: [c1, c2] };
    expect(countConditions(group)).toBe(2);
    expect(flattenConditions(group)).toEqual([c1, c2]);
  });

  it("중첩 그룹도 재귀적으로 센다", () => {
    const c1: Condition = { left: { kind: "rsi", period: 14 }, op: "<", right: { kind: "const", value: 30 } };
    const c2: Condition = { left: { kind: "sma", period: 5 }, op: ">", right: { kind: "sma", period: 20 } };
    const c3: Condition = {
      left: { kind: "macd_line", fast: 12, slow: 26, signal: 9 },
      op: ">",
      right: { kind: "macd_signal", fast: 12, slow: 26, signal: 9 },
    };
    const nested: ConditionGroup = {
      combinator: "or",
      children: [c1, { combinator: "and", children: [c2, c3] }],
    };
    expect(countConditions(nested)).toBe(3);
    expect(flattenConditions(nested)).toEqual([c1, c2, c3]);
  });
});

describe("formatNode", () => {
  const rsiLt30: Condition = {
    left: { kind: "rsi", period: 14 },
    op: "<",
    right: { kind: "const", value: 30 },
  };
  const smaCross: Condition = {
    left: { kind: "sma", period: 5 },
    op: ">",
    right: { kind: "sma", period: 20 },
  };
  const macdCross: Condition = {
    left: { kind: "macd_line", fast: 12, slow: 26, signal: 9 },
    op: ">",
    right: { kind: "macd_signal", fast: 12, slow: 26, signal: 9 },
  };

  it("단일 조건은 그대로 수식 문자열을 반환한다", () => {
    expect(formatNode(rsiLt30)).toBe("RSI(14) < 30");
  });

  it("AND 결합", () => {
    const group: ConditionGroup = { combinator: "and", children: [smaCross, macdCross] };
    expect(formatNode(group, true)).toBe(
      "SMA(5) > SMA(20)  AND  MACD(12,26,9) > Signal(12,26,9)",
    );
  });

  it("OR 결합", () => {
    const group: ConditionGroup = { combinator: "or", children: [rsiLt30, smaCross] };
    expect(formatNode(group, true)).toBe("RSI(14) < 30  OR  SMA(5) > SMA(20)");
  });

  it("최상위(top=true)는 괄호를 생략한다", () => {
    const group: ConditionGroup = { combinator: "or", children: [rsiLt30, smaCross] };
    expect(formatNode(group, true)).not.toMatch(/^\(/);
  });

  it("중첩 그룹은 괄호로 묶인다(최상위가 아닐 때)", () => {
    const nested: ConditionGroup = {
      combinator: "or",
      children: [rsiLt30, { combinator: "and", children: [smaCross, macdCross] }],
    };
    expect(formatNode(nested, true)).toBe(
      "RSI(14) < 30  OR  (SMA(5) > SMA(20)  AND  MACD(12,26,9) > Signal(12,26,9))",
    );
  });

  it("자식이 1개뿐인 그룹은 괄호를 생략한다", () => {
    const single: ConditionGroup = { combinator: "and", children: [rsiLt30] };
    const wrapper: ConditionGroup = { combinator: "or", children: [single, smaCross] };
    expect(formatNode(wrapper, true)).toBe("RSI(14) < 30  OR  SMA(5) > SMA(20)");
  });

  it("자식이 없는 그룹은 (없음)을 표시한다", () => {
    const empty: ConditionGroup = { combinator: "and", children: [] };
    expect(formatNode(empty, true)).toBe("(없음)");
  });
});

describe("formatCustomFormula", () => {
  it("entry/exit 각각 최상위 논리식 문자열을 반환한다", () => {
    const entry: ConditionGroup = {
      combinator: "and",
      children: [
        { left: { kind: "sma", period: 5 }, op: ">", right: { kind: "sma", period: 20 } },
      ],
    };
    const exit: ConditionGroup = {
      combinator: "and",
      children: [
        { left: { kind: "sma", period: 5 }, op: "<", right: { kind: "sma", period: 20 } },
      ],
    };
    expect(formatCustomFormula({ entry, exit })).toEqual({
      entry: "SMA(5) > SMA(20)",
      exit: "SMA(5) < SMA(20)",
    });
  });
});

describe("defaultConfig", () => {
  it("sma_crossover 기본값을 생성한다", () => {
    const cfg = defaultConfig("sma_crossover");
    expect(cfg).toMatchObject({ type: "sma_crossover", fast: 5, slow: 20, symbol: "005930" });
  });

  it("공통 필드(common)를 유지하며 유형을 전환한다", () => {
    const cfg = defaultConfig("rsi", { symbol: "000660", cash: 5_000_000 });
    expect(cfg).toMatchObject({ type: "rsi", symbol: "000660", cash: 5_000_000, period: 14 });
  });

  it("rebalance 는 단일 종목 공통 필드를 쓰지 않는 별도 형태다", () => {
    const cfg = defaultConfig("rebalance");
    expect(cfg.type).toBe("rebalance");
    if (cfg.type === "rebalance") {
      expect(cfg.universe.length).toBeGreaterThan(0);
      expect(cfg.cadence).toBe("monthly");
    }
  });
});
